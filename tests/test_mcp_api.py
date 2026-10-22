"""End-to-end checks for the Nexus OAuth flow and the read-only /api/v1 endpoints.

Runs against a throwaway SQLite copy of a Nexus database:

    NEXUS_TEST_DB=/path/to/copy/sales_history.db python -m pytest tests/test_mcp_api.py -q

Add NEXUS_TEST_DATABASE_URL=postgresql://... to run the same checks on an empty
Postgres database (trips are copied in from the SQLite file).

The test creates two users (an admin and a sales rep), walks the full OAuth
authorisation-code + PKCE flow with Flask's test client, then exercises every
API endpoint, including territory scoping.
"""

import base64
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SOURCE_DB = os.environ.get("NEXUS_TEST_DB", "")
REDIRECT = "http://localhost:8765/callback"


PG_URL = os.environ.get("NEXUS_TEST_DATABASE_URL", "")


def _copy_trips_from_sqlite(db_connect, source):
    """Give an empty Postgres test database some real trips/schedules from a SQLite copy."""
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    with db_connect() as connection:
        if connection.execute("SELECT COUNT(*) FROM trip_plans").fetchone()[0]:
            return
        for table in ("trip_plans", "trip_schedules"):
            target_cols = {r[1] for r in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            for row in src.execute(f"SELECT * FROM {table}").fetchall():
                row = dict(row)
                cols = [c for c in row if c in target_cols]
                marks = ",".join("?" for _ in cols)
                connection.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", [row[c] for c in cols])
        connection.commit()


@pytest.fixture(scope="module")
def nexus():
    if not SOURCE_DB or not Path(SOURCE_DB).exists():
        pytest.skip("Set NEXUS_TEST_DB to a copy of a Nexus SQLite database.")
    data_dir = tempfile.mkdtemp(prefix="nexus-test-")
    shutil.copy(SOURCE_DB, Path(data_dir) / "sales_history.db")
    os.environ.update({
        "DATA_DIR": data_dir,
        "SECRET_KEY": "test-secret",
        "MCP_ALLOW_LOCALHOST_REDIRECTS": "1",
        "NEXUS_PUBLIC_URL": "https://nexus.test",
    })
    if PG_URL:
        os.environ["DATABASE_URL"] = PG_URL
        os.environ.pop("FORCE_SQLITE", None)
    else:
        os.environ["FORCE_SQLITE"] = "1"
        os.environ.pop("DATABASE_URL", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app as nexus_app  # noqa: E402
    from core.db import db_connect

    if PG_URL:
        _copy_trips_from_sqlite(db_connect, SOURCE_DB)

    from werkzeug.security import generate_password_hash
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as db:
        for email, name, role in (("admin@test.nz", "Admin", "admin"), ("rep@test.nz", "Rep One", "sales_rep")):
            if not db.execute("SELECT id FROM app_users WHERE email=?", (email,)).fetchone():
                db.execute(
                    "INSERT INTO app_users (email,display_name,password_hash,role,active,created_at,last_login_at,os_access) VALUES (?,?,?,?,1,?,'',0)",
                    (email, name, generate_password_hash("pass123"), role, now),
                )
        rep_id = db.execute("SELECT id FROM app_users WHERE email='rep@test.nz'").fetchone()[0]
        admin_id = db.execute("SELECT id FROM app_users WHERE email='admin@test.nz'").fetchone()[0]
        ids = [r[0] for r in db.execute("SELECT id FROM route_customers ORDER BY id").fetchall()]
        # First half belongs to the rep, a few to someone else, rest unassigned.
        half = len(ids) // 2
        db.execute("UPDATE route_customers SET assigned_user_id=NULL")
        for cid in ids[:half]:
            db.execute("UPDATE route_customers SET assigned_user_id=? WHERE id=?", (rep_id, cid))
        for cid in ids[half:half + 5]:
            db.execute("UPDATE route_customers SET assigned_user_id=? WHERE id=?", (admin_id, cid))
        trip_ids = [r[0] for r in db.execute("SELECT id FROM trip_plans ORDER BY id").fetchall()]
        if trip_ids:
            db.execute("UPDATE trip_plans SET owner_user_id=? WHERE id=?", (rep_id, trip_ids[0]))
            for tid in trip_ids[1:]:
                db.execute("UPDATE trip_plans SET owner_user_id=? WHERE id=?", (admin_id, tid))
        db.commit()
    nexus_app.app.config["TESTING"] = True
    return {"app": nexus_app.app, "rep_id": rep_id, "admin_id": admin_id, "customer_ids": ids,
            "half": half, "trip_ids": trip_ids, "data_dir": data_dir}


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _token_for(app, email):
    client = app.test_client()
    meta = client.get("/.well-known/oauth-authorization-server").get_json()
    assert meta["issuer"] == "https://nexus.test"
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude test"})
    assert reg.status_code == 201, reg.data
    client_id = reg.get_json()["client_id"]
    verifier, challenge = _pkce()
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "scope": "nexus.read", "resource": "https://mcp.test/mcp",
    }
    # Not signed in -> sent to the normal Nexus login, which returns to authorize.
    first = client.get("/oauth/authorize", query_string=params)
    assert first.status_code == 302 and "/login" in first.headers["Location"]
    next_path = parse_qs(urlparse(first.headers["Location"]).query)["next"][0]
    login = client.post("/login", data={"email": email, "password": "pass123", "next": next_path})
    assert login.status_code == 302 and login.headers["Location"].startswith("/oauth/authorize")
    consent = client.get(login.headers["Location"])
    assert consent.status_code == 200 and b"Allow" in consent.data
    csrf = re.search(rb'name="csrf" value="([^"]+)"', consent.data).group(1).decode()
    approved = client.post("/oauth/authorize", data={**params, "csrf": csrf, "decision": "allow"})
    assert approved.status_code == 302
    location = approved.headers["Location"]
    assert location.startswith(REDIRECT)
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]
    bad = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                             "redirect_uri": REDIRECT, "code_verifier": "wrong"})
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_grant"
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                             "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tok.status_code == 200, tok.data
    body = tok.get_json()
    body["_replay"] = {"code": code, "verifier": verifier}
    return body, client_id


def test_register_rejects_unknown_redirect(nexus):
    client = nexus["app"].test_client()
    res = client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert res.status_code == 400


def test_api_requires_bearer(nexus):
    client = nexus["app"].test_client()
    res = client.get("/api/v1/whoami")
    assert res.status_code == 401
    assert "Bearer" in res.headers.get("WWW-Authenticate", "")
    assert client.get("/api/v1/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_full_flow_rep_scoping(nexus):
    app = nexus["app"]
    tokens, client_id = _token_for(app, "rep@test.nz")
    client = app.test_client()
    h = {"Authorization": f"Bearer {tokens['access_token']}"}

    me_res = client.get("/api/v1/whoami", headers=h)
    assert me_res.status_code == 200, me_res.data
    me = me_res.get_json()
    assert me["user"]["email"] == "rep@test.nz" and me["customer_scope"] == "own_and_unassigned"

    customers = client.get("/api/v1/customers?limit=1000", headers=h).get_json()
    visible_ids = {c["customer_id"] for c in customers["customers"]}
    other = set(nexus["customer_ids"][nexus["half"]:nexus["half"] + 5])
    assert visible_ids and not (visible_ids & other)
    first = customers["customers"][0]
    for field in ("lat", "lng", "priority_tier", "call_interval_days", "next_due", "open_time", "call_minutes", "cycle_status"):
        assert field in first

    detail = client.get(f"/api/v1/customers/{first['customer_id']}", headers=h)
    assert detail.status_code == 200
    assert {"contacts", "recent_visits", "open_actions"} <= set(detail.get_json())
    if other:
        assert client.get(f"/api/v1/customers/{sorted(other)[0]}", headers=h).status_code == 404

    tiers = client.get("/api/v1/customers?tiers=A,B&due_within_days=14", headers=h)
    assert tiers.status_code == 200

    pair = [c["customer_id"] for c in customers["customers"][:3]]
    dt = client.post("/api/v1/drive-times", headers=h, json={"customer_ids": pair})
    assert dt.status_code == 200, dt.data
    body = dt.get_json()
    assert body["source"] in {"road", "partial", "estimate"}
    assert len(body["minutes"]) == len(pair) and len(body["minutes"][0]) == len(pair)
    if other:
        blocked = client.post("/api/v1/drive-times", headers=h, json={"customer_ids": [sorted(other)[0], pair[0]]})
        assert blocked.status_code == 404

    trips = client.get("/api/v1/trips?from=2000-01-01&to=2100-01-01", headers=h).get_json()["trips"]
    assert {t["trip_id"] for t in trips} <= {nexus["trip_ids"][0]} if nexus["trip_ids"] else True
    if nexus["trip_ids"]:
        own = client.get(f"/api/v1/trips/{nexus['trip_ids'][0]}", headers=h)
        assert own.status_code == 200 and "days" in own.get_json()
        if len(nexus["trip_ids"]) > 1:
            assert client.get(f"/api/v1/trips/{nexus['trip_ids'][1]}", headers=h).status_code == 404

    acts = client.get("/api/v1/actions?status=all", headers=h)
    assert acts.status_code == 200
    assert client.get("/api/v1/start-locations", headers=h).status_code == 200

    # Refresh rotates: new pair works, old refresh token is dead.
    r1 = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert r1.status_code == 200
    r2 = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert r2.status_code == 400
    new_h = {"Authorization": f"Bearer {r1.get_json()['access_token']}"}
    assert client.get("/api/v1/whoami", headers=new_h).status_code == 200

    # Revoke kills the access token.
    client.post("/oauth/revoke", data={"token": r1.get_json()["access_token"]})
    assert client.get("/api/v1/whoami", headers=new_h).status_code == 401


def test_admin_sees_all_and_trip_detail(nexus):
    app = nexus["app"]
    tokens, _ = _token_for(app, "admin@test.nz")
    client = app.test_client()
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    customers = client.get("/api/v1/customers?limit=1000", headers=h).get_json()
    assert customers["total"] == len(nexus["customer_ids"])
    trips = client.get("/api/v1/trips?from=2000-01-01&to=2100-01-01", headers=h).get_json()["trips"]
    assert len(trips) == len(nexus["trip_ids"])
    with_stops = 0
    for trip in trips:
        detail = client.get(f"/api/v1/trips/{trip['trip_id']}", headers=h).get_json()
        for day in detail["days"]:
            for stop in day["stops"]:
                assert len(stop["appointment_id"]) == 40
                with_stops += 1
    assert with_stops > 0


def test_pilot_allowlist_blocks_others(nexus, monkeypatch):
    monkeypatch.setenv("MCP_PILOT_EMAILS", "someone-else@test.nz")
    with pytest.raises(AssertionError):
        _token_for(nexus["app"], "rep@test.nz")


def test_session_cookie_is_not_enough_for_api(nexus):
    client = nexus["app"].test_client()
    client.post("/login", data={"email": "admin@test.nz", "password": "pass123", "next": "/"})
    assert client.get("/api/v1/whoami").status_code == 401


def test_code_replay_revokes_tokens(nexus):
    app = nexus["app"]
    tokens, client_id = _token_for(app, "rep@test.nz")
    client = app.test_client()
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get("/api/v1/whoami", headers=h).status_code == 200
    again = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": tokens["_replay"]["code"],
                                               "client_id": client_id, "redirect_uri": REDIRECT,
                                               "code_verifier": tokens["_replay"]["verifier"]})
    assert again.status_code == 400
    # Replaying a code is treated as theft: the tokens it produced stop working.
    assert client.get("/api/v1/whoami", headers=h).status_code == 401
