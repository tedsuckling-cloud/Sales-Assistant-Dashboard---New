"""OAuth 2.1 sign-in so Claude (or another MCP client) can act as a Nexus user.

Nexus is the authorisation server: people sign in with their normal Nexus
login and approve access on a consent page. The Nexus MCP service and the
/api/v1 endpoints accept the resulting bearer tokens.

Flow (RFC 6749 + PKCE RFC 7636, dynamic client registration RFC 7591,
metadata RFC 8414, resource indicators RFC 8707):

    GET  /.well-known/oauth-authorization-server   metadata
    POST /oauth/register                           client registration
    GET  /oauth/authorize                          consent page (needs Nexus login)
    POST /oauth/authorize                          approve / deny
    POST /oauth/token                              code or refresh -> tokens
    POST /oauth/revoke                             revoke a token

Only SHA-256 hashes of codes and tokens are stored.

Environment:
    NEXUS_PUBLIC_URL               https://<nexus host>  (issuer; defaults to request host)
    MCP_ALLOWED_REDIRECT_URIS      comma list of allowed redirect URI prefixes
    MCP_ALLOW_LOCALHOST_REDIRECTS  "1" to allow http://localhost / 127.0.0.1 (testing only)
    MCP_PILOT_EMAILS               comma list; when set, only these users may approve access
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlparse

from flask import Blueprint, abort, g, jsonify, make_response, render_template_string, request, session

READ_SCOPE = "nexus.read"
SUPPORTED_SCOPES = [READ_SCOPE]
CODE_TTL_SECONDS = 600
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 60 * 24 * 3600
DEFAULT_REDIRECT_PREFIXES = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

# Endpoints that must work without a Nexus browser session. /oauth/authorize is
# deliberately NOT here: it relies on the normal login redirect.
PUBLIC_OAUTH_ENDPOINTS = {
    "nexus_oauth.metadata",
    "nexus_oauth.register",
    "nexus_oauth.token",
    "nexus_oauth.revoke",
}


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _now():
    return int(time.time())


def ensure_oauth_schema(db_connect):
    with db_connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT NOT NULL DEFAULT '',
                redirect_uris TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                resource TEXT NOT NULL DEFAULT '',
                expires_at BIGINT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
                token_hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                client_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                resource TEXT NOT NULL DEFAULT '',
                expires_at BIGINT NOT NULL,
                created_at BIGINT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                last_used_at BIGINT NOT NULL DEFAULT 0
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_user ON mcp_oauth_tokens(user_id)")
        connection.commit()


def issuer_url():
    configured = (os.environ.get("NEXUS_PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


def _allowed_redirect(uri):
    uri = str(uri or "").strip()
    if not uri:
        return False
    parsed = urlparse(uri)
    if parsed.fragment:
        return False
    if os.environ.get("MCP_ALLOW_LOCALHOST_REDIRECTS", "").strip() == "1":
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
            return True
    configured = [p.strip() for p in (os.environ.get("MCP_ALLOWED_REDIRECT_URIS") or "").split(",") if p.strip()]
    prefixes = configured or list(DEFAULT_REDIRECT_PREFIXES)
    return parsed.scheme == "https" and any(uri.startswith(prefix) for prefix in prefixes)


def _pilot_allows(email):
    configured = {e.strip().lower() for e in (os.environ.get("MCP_PILOT_EMAILS") or "").split(",") if e.strip()}
    return not configured or str(email or "").strip().lower() in configured


def _normalise_scope(raw):
    requested = [s for s in str(raw or "").split() if s]
    granted = [s for s in requested if s in SUPPORTED_SCOPES]
    return " ".join(granted or [READ_SCOPE])


def _with_query(uri, params):
    joiner = "&" if "?" in uri else "?"
    return uri + joiner + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def _token_error(error, description="", status=400):
    response = make_response(jsonify({"error": error, "error_description": description}), status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def lookup_bearer(db_connect, raw_token):
    """Return ``(user_dict, token_row_dict)`` for a live access token, else ``(None, None)``."""
    raw_token = str(raw_token or "").strip()
    if not raw_token:
        return None, None
    now = _now()
    with db_connect() as connection:
        connection.row_factory = sqlite3.Row
        token = connection.execute(
            "SELECT * FROM mcp_oauth_tokens WHERE token_hash=? AND kind='access'",
            (_hash(raw_token),),
        ).fetchone()
        if not token:
            return None, None
        token = dict(token)
        if int(token.get("revoked") or 0) or int(token.get("expires_at") or 0) <= now:
            return None, None
        user = connection.execute(
            "SELECT id,email,display_name,role,active,created_at,last_login_at,os_access FROM app_users WHERE id=?",
            (token["user_id"],),
        ).fetchone()
        if not user or not int(dict(user).get("active") or 0):
            return None, None
        # Record use at most once a minute to avoid a write on every call.
        if now - int(token.get("last_used_at") or 0) > 60:
            connection.execute("UPDATE mcp_oauth_tokens SET last_used_at=? WHERE token_hash=?", (now, token["token_hash"]))
            connection.commit()
        return dict(user), token


CONSENT_PAGE = r"""
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allow access · Riverlea Nexus</title>
<style>
:root{font-family:Montserrat,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#3F3F3E}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#F4F6F6}
.card{width:min(460px,100%);background:#fff;border:1px solid #dfe6ec;border-radius:18px;padding:28px;box-shadow:0 20px 60px rgba(28,47,63,.10)}
h1{font-size:22px;margin:0 0 10px}p{color:#667581;line-height:1.5;margin:0 0 14px}
ul{margin:0 0 18px;padding-left:20px;line-height:1.6}
.row{display:flex;gap:10px;margin-top:18px}
button{flex:1;border:0;border-radius:10px;padding:12px 14px;font-weight:700;font-size:15px;cursor:pointer}
.allow{background:#3F3F3E;color:#fff}.deny{background:#eef1f3;color:#3F3F3E}
.who{font-size:13px;color:#667581}.err{background:#fdecea;color:#8a1c12;padding:10px 12px;border-radius:10px;margin-bottom:12px}
</style></head><body><main class="card">
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<h1>Allow {{ client_name }} to use Nexus?</h1>
<p>It will act as you, <strong>{{ user_name }}</strong>, with the same access you have in Nexus.</p>
<ul>
<li>Read your customers, contacts and call cycles</li>
<li>Read your trips, appointments and visit history</li>
<li>Read your actions and saved start locations</li>
<li>Get drive times between locations</li>
</ul>
<p>It cannot change or delete anything. You can revoke access at any time.</p>
{% if not error %}
<form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}">
{% for k, v in params.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
<div class="row"><button class="deny" name="decision" value="deny">Deny</button><button class="allow" name="decision" value="allow">Allow</button></div>
</form>
{% endif %}
<p class="who">Signed in as {{ user_email }}</p>
</main></body></html>
"""

ERROR_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign-in problem · Riverlea Nexus</title></head>
<body style="font-family:system-ui,sans-serif;padding:32px;color:#3F3F3E"><h1 style="font-size:20px">This sign-in link can't be used</h1>
<p>{{ message }}</p></body></html>"""


def register_oauth_routes(app, *, db_connect, audit_event):
    ensure_oauth_schema(db_connect)
    bp = Blueprint("nexus_oauth", __name__)

    def load_client(connection, client_id):
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM mcp_oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        client = dict(row)
        try:
            client["redirect_uris"] = json.loads(client.get("redirect_uris") or "[]")
        except ValueError:
            client["redirect_uris"] = []
        return client

    @bp.route("/.well-known/oauth-authorization-server", methods=["GET"], endpoint="metadata")
    def metadata():
        base = issuer_url()
        response = jsonify({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": SUPPORTED_SCOPES,
        })
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @bp.route("/oauth/register", methods=["POST"], endpoint="register")
    def register():
        payload = request.get_json(silent=True) or {}
        redirect_uris = payload.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _token_error("invalid_redirect_uri", "redirect_uris is required.")
        bad = [u for u in redirect_uris if not _allowed_redirect(u)]
        if bad:
            return _token_error("invalid_redirect_uri", "This redirect address is not allowed for Nexus.")
        method = payload.get("token_endpoint_auth_method") or "none"
        if method != "none":
            return _token_error("invalid_client_metadata", "Only public clients (token_endpoint_auth_method=none) are supported.")
        client_id = "nxc_" + secrets.token_urlsafe(18)
        name = str(payload.get("client_name") or "MCP client")[:120]
        issued = _now()
        with db_connect() as connection:
            connection.execute(
                "INSERT INTO mcp_oauth_clients (client_id,client_name,redirect_uris,created_at) VALUES (?,?,?,?)",
                (client_id, name, json.dumps(redirect_uris), issued),
            )
            connection.commit()
        response = make_response(jsonify({
            "client_id": client_id,
            "client_id_issued_at": issued,
            "client_name": name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": READ_SCOPE,
        }), 201)
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/oauth/authorize", methods=["GET", "POST"], endpoint="authorize")
    def authorize():
        # The app-wide login check has already redirected anonymous visitors to /login.
        user = getattr(g, "current_user", None)
        if not user:
            abort(401)
        params = {
            key: str(request.values.get(key) or "")
            for key in ("response_type", "client_id", "redirect_uri", "code_challenge",
                        "code_challenge_method", "state", "scope", "resource")
        }
        with db_connect() as connection:
            client = load_client(connection, params["client_id"])
        if not client:
            return render_template_string(ERROR_PAGE, message="The app asking for access is not registered with Nexus. Try connecting again."), 400
        redirect_uri = params["redirect_uri"]
        if redirect_uri not in client["redirect_uris"]:
            return render_template_string(ERROR_PAGE, message="The return address does not match the registered app."), 400

        def bounce(**extra):
            from flask import redirect as flask_redirect
            return flask_redirect(_with_query(redirect_uri, {**extra, "state": params["state"]}))

        if params["response_type"] != "code":
            return bounce(error="unsupported_response_type")
        if params["code_challenge_method"] != "S256" or not params["code_challenge"]:
            return bounce(error="invalid_request", error_description="PKCE with S256 is required.")

        context = dict(
            client_name=client.get("client_name") or "This app",
            user_name=user.get("display_name") or user.get("email"),
            user_email=user.get("email"),
            params=params,
            error="",
        )
        if not _pilot_allows(user.get("email")):
            context["error"] = "Your Nexus account is not in the Claude pilot yet. Ask Ted to add you."
            return render_template_string(CONSENT_PAGE, csrf="", **context), 403

        if request.method == "GET":
            csrf = secrets.token_urlsafe(24)
            session["mcp_oauth_csrf"] = csrf
            page = make_response(render_template_string(CONSENT_PAGE, csrf=csrf, **context))
            # The Allow button must never be clickable inside someone else's frame.
            page.headers["X-Frame-Options"] = "DENY"
            page.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            page.headers["Cache-Control"] = "no-store"
            return page

        expected = session.pop("mcp_oauth_csrf", None)
        if not expected or not secrets.compare_digest(str(expected), str(request.form.get("csrf") or "")):
            return render_template_string(ERROR_PAGE, message="This approval page expired. Go back and connect again."), 400
        if request.form.get("decision") != "allow":
            audit_event("mcp_oauth_denied", f"{client.get('client_name')} denied by {user.get('email')}")
            return bounce(error="access_denied")

        code = secrets.token_urlsafe(32)
        with db_connect() as connection:
            connection.execute(
                "INSERT INTO mcp_oauth_codes (code_hash,client_id,user_id,redirect_uri,code_challenge,scope,resource,expires_at,used) VALUES (?,?,?,?,?,?,?,?,0)",
                (_hash(code), client["client_id"], int(user["id"]), redirect_uri, params["code_challenge"],
                 _normalise_scope(params["scope"]), params["resource"], _now() + CODE_TTL_SECONDS),
            )
            connection.commit()
        audit_event("mcp_oauth_approved", f"{client.get('client_name')} approved by {user.get('email')}")
        return bounce(code=code)

    def issue_tokens(connection, client_id, user_id, scope, resource):
        now = _now()
        access = "nxa_" + secrets.token_urlsafe(32)
        refresh = "nxr_" + secrets.token_urlsafe(32)
        for raw, kind, ttl in ((access, "access", ACCESS_TTL_SECONDS), (refresh, "refresh", REFRESH_TTL_SECONDS)):
            connection.execute(
                "INSERT INTO mcp_oauth_tokens (token_hash,kind,client_id,user_id,scope,resource,expires_at,created_at,revoked,last_used_at) VALUES (?,?,?,?,?,?,?,?,0,0)",
                (_hash(raw), kind, client_id, int(user_id), scope, resource or "", now + ttl, now),
            )
        connection.commit()
        response = make_response(jsonify({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": scope,
        }))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    @bp.route("/oauth/token", methods=["POST"], endpoint="token")
    def token():
        form = request.form
        grant_type = form.get("grant_type", "")
        client_id = form.get("client_id", "")
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            client = load_client(connection, client_id)
            if not client:
                return _token_error("invalid_client", "Unknown client.", 401)

            if grant_type == "authorization_code":
                code = form.get("code", "")
                row = connection.execute("SELECT * FROM mcp_oauth_codes WHERE code_hash=?", (_hash(code),)).fetchone()
                if not row:
                    return _token_error("invalid_grant", "Unknown code.")
                row = dict(row)
                if int(row.get("used") or 0):
                    # A replayed code: revoke everything it produced.
                    connection.execute(
                        "UPDATE mcp_oauth_tokens SET revoked=1 WHERE client_id=? AND user_id=?",
                        (row["client_id"], row["user_id"]),
                    )
                    connection.commit()
                    return _token_error("invalid_grant", "Code already used.")
                if row["client_id"] != client_id or row["redirect_uri"] != form.get("redirect_uri", ""):
                    return _token_error("invalid_grant", "Code was issued to a different client or redirect.")
                if int(row["expires_at"]) <= _now():
                    return _token_error("invalid_grant", "Code expired.")
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "ignore")).digest()).rstrip(b"=").decode("ascii")
                if not verifier or not secrets.compare_digest(challenge, row["code_challenge"]):
                    return _token_error("invalid_grant", "PKCE check failed.")
                resource = form.get("resource", "") or row.get("resource") or ""
                connection.execute("UPDATE mcp_oauth_codes SET used=1 WHERE code_hash=?", (row["code_hash"],))
                connection.commit()
                return issue_tokens(connection, client_id, row["user_id"], row["scope"] or READ_SCOPE, resource)

            if grant_type == "refresh_token":
                raw = form.get("refresh_token", "")
                row = connection.execute(
                    "SELECT * FROM mcp_oauth_tokens WHERE token_hash=? AND kind='refresh'", (_hash(raw),)
                ).fetchone()
                if not row:
                    return _token_error("invalid_grant", "Unknown refresh token.")
                row = dict(row)
                if row["client_id"] != client_id or int(row.get("revoked") or 0) or int(row["expires_at"]) <= _now():
                    return _token_error("invalid_grant", "Refresh token is no longer valid.")
                user = connection.execute("SELECT active,email FROM app_users WHERE id=?", (row["user_id"],)).fetchone()
                if not user or not int(dict(user).get("active") or 0) or not _pilot_allows(dict(user).get("email")):
                    return _token_error("invalid_grant", "User no longer has access.")
                # Rotate: the old refresh token can't be used again.
                connection.execute("UPDATE mcp_oauth_tokens SET revoked=1 WHERE token_hash=?", (row["token_hash"],))
                connection.commit()
                return issue_tokens(connection, client_id, row["user_id"], row["scope"] or READ_SCOPE, row.get("resource") or "")

        return _token_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    @bp.route("/oauth/revoke", methods=["POST"], endpoint="revoke")
    def revoke():
        raw = request.form.get("token", "")
        if raw:
            with db_connect() as connection:
                connection.execute("UPDATE mcp_oauth_tokens SET revoked=1 WHERE token_hash=?", (_hash(raw),))
                connection.commit()
        response = make_response("", 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(bp)
    return bp
