"""Read-only JSON API (/api/v1) used by the Nexus MCP service.

Every request needs ``Authorization: Bearer <token>`` from the Nexus OAuth
flow (nexus_api/oauth.py). The token's user becomes ``g.current_user`` so the
same territory and ownership rules as the Nexus screens apply:

* sales reps and managers see customers assigned to them (or unassigned),
  their own trips and their own (or unowned) actions;
* admins see everything, and may filter by ``owner``.

Phase 1 is read-only. Nothing here writes business data; the only writes are
token bookkeeping and the audit row for drive-time requests.
"""

import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, g, jsonify, request

from core.routing import MAX_POINTS, road_matrix
from nexus_api.oauth import READ_SCOPE, issuer_url, lookup_bearer

API_VERSION = "1.0"
DONE_ACTION_STATUSES = {"completed", "done", "closed", "cancelled"}
KIND_NAMES = {"locked": "fixed", "leeway": "leeway", "preferred": "leeway", "flexible": "flexible"}


def _parse_day(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt, length in (("%Y-%m-%d", 10), ("%d/%m/%Y", 10), ("%Y%m%d", 8), ("%d-%m-%Y", 10)):
        try:
            return datetime.strptime(text[:length], fmt).date()
        except ValueError:
            continue
    return None


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else (value or None)


def _int(value, default=None, low=None, high=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key(name):
    return str(name or "").strip().casefold()


def _duration_from_note(note):
    """Older schedules only record the call length in the note, e.g. '45 minute visit'."""
    match = re.search(r"(\d{1,3})\s*minute visit", str(note or ""), flags=re.I)
    return int(match.group(1)) if match else None


def _error(status, code, message):
    response = jsonify({"error": code, "message": message})
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = f'Bearer realm="nexus", error="{code}"'
    return response


def register_api_routes(
    app, *, db_connect, nz_today, territory_user_id, private_workspace_user_id, trip_owner_user_id,
    trip_row_for_current_user, customer_link_clause, parse_schedule_appointments, appointment_uid,
    active_next_visit_requirement, get_route_defaults, audit_event,
    latest_customer_visit_dates, customer_visit_cycle_state,
):
    bp = Blueprint("nexus_api", __name__, url_prefix="/api/v1")

    # ------------------------------------------------------------------ auth
    @bp.before_request
    def require_bearer():
        if request.method == "OPTIONS":
            return None
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return _error(401, "invalid_token", "A Nexus bearer token is required.")
        user, token = lookup_bearer(db_connect, header[7:].strip())
        if not user:
            return _error(401, "invalid_token", "The token is missing, expired or revoked.")
        scopes = set(str(token.get("scope") or "").split())
        if READ_SCOPE not in scopes:
            return _error(403, "insufficient_scope", f"This token does not include {READ_SCOPE}.")
        # Session users are ignored on the API: the bearer token is the only identity.
        g.current_user = user
        g.api_token = token
        g.api_scopes = scopes
        return None

    @bp.errorhandler(404)
    def not_found(_exc):
        return _error(404, "not_found", "Not found, or not visible to you.")

    @bp.errorhandler(400)
    def bad_request(exc):
        return _error(400, "bad_request", getattr(exc, "description", "Bad request."))

    # --------------------------------------------------------------- helpers
    def is_admin():
        return (g.current_user or {}).get("role") == "admin"

    def scope_owner_filter():
        """Owner id to filter by, or None for everything (admins only)."""
        own = territory_user_id()
        if own is not None:
            return own
        owner = str(request.args.get("owner") or "all").strip().lower()
        if owner in {"", "all"}:
            return None
        if owner == "me":
            return int(g.current_user["id"])
        return _int(owner)

    def visible_customer_rows(connection):
        connection.row_factory = sqlite3.Row
        owner = scope_owner_filter()
        if owner is None:
            rows = connection.execute("SELECT * FROM route_customers ORDER BY lower(name)").fetchall()
        elif territory_user_id() is not None:
            rows = connection.execute(
                "SELECT * FROM route_customers WHERE assigned_user_id=? OR assigned_user_id IS NULL ORDER BY lower(name)",
                (owner,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM route_customers WHERE assigned_user_id=? ORDER BY lower(name)", (owner,)
            ).fetchall()
        return [dict(r) for r in rows]

    def visible_customer(connection, customer_id):
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM route_customers WHERE id=?", (customer_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        own = territory_user_id()
        assigned = _int(row.get("assigned_user_id"))
        if own is not None and assigned is not None and assigned != own:
            return None
        return row

    def notify_contact_index(connection):
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM customer_contacts WHERE notify_before_visit=1"
            ).fetchall()
        except Exception:
            return {}, {}
        by_id, by_name = {}, {}
        for r in rows:
            r = dict(r)
            if r.get("customer_id"):
                by_id.setdefault(int(r["customer_id"]), []).append(r)
            else:
                by_name.setdefault(_key(r.get("store_name")), []).append(r)
        return by_id, by_name

    def customer_summary(row, today, notify=None, latest=None):
        """``latest`` is the entry from latest_customer_visit_dates (the same source the
        Nexus customer list uses: On the Road, history imports, manual corrections)."""
        last_visit = latest["date"] if latest else _parse_day(row.get("last_call"))
        state = customer_visit_cycle_state(last_visit, row.get("call_interval"), today)
        interval = state["interval"]
        next_due = (last_visit + timedelta(days=interval)) if last_visit else None
        days_overdue = (today - next_due).days if next_due else None
        summary = {
            "customer_id": int(row["id"]),
            "name": row.get("name") or "",
            "address": {
                "street": row.get("street") or "",
                "city": row.get("city") or "",
                "postcode": row.get("zip") or "",
                "country": row.get("country") or "",
            },
            "lat": _float(row.get("latitude")),
            "lng": _float(row.get("longitude")),
            "customer_group": row.get("customer_group") or "",
            "categories": row.get("categories") or "",
            "priority_tier": (row.get("priority_tier") or "C").strip().upper() or "C",
            "call_interval_days": interval,
            "last_visit": _iso(last_visit),
            "last_visit_source": (latest or {}).get("source") or ("route baseline" if last_visit else None),
            "cycle_status": state["urgency"],
            "next_due": _iso(next_due),
            "days_overdue": days_overdue,
            "never_visited": last_visit is None,
            "call_minutes": _int(row.get("ideal_call_minutes"), 45, 5),
            "open_time": row.get("customer_open_time") or "08:00",
            "close_time": row.get("customer_close_time") or "17:00",
            "preferred_visit_time": row.get("preferred_visit_time") or "",
            "preferred_visit_leeway_minutes": _int(row.get("preferred_visit_leeway_minutes"), 60, 0),
            "urgency": row.get("urgency") or "",
            "phone": row.get("phone") or "",
            "email": row.get("email") or "",
            "assigned_user_id": _int(row.get("assigned_user_id")),
            "external_id": row.get("external_id") or "",
            "hubspot_company_id": row.get("hubspot_company_id") or "",
        }
        if notify is not None:
            summary["notify_before_visit_contacts"] = [
                {"name": c.get("contact_name") or "", "email": c.get("email") or "", "phone": c.get("phone") or ""}
                for c in notify
            ]
        return summary

    def latest_schedule(connection, trip_id):
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1", (trip_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- endpoints
    @bp.route("/", methods=["GET"], endpoint="index")
    def index():
        return jsonify({
            "api_version": API_VERSION,
            "endpoints": [
                "GET /api/v1/whoami",
                "GET /api/v1/customers",
                "GET /api/v1/customers/<customer_id>",
                "GET /api/v1/start-locations",
                "POST /api/v1/drive-times",
                "GET /api/v1/trips",
                "GET /api/v1/trips/<trip_id>",
                "GET /api/v1/actions",
            ],
        })

    @bp.route("/whoami", methods=["GET"], endpoint="whoami")
    def whoami():
        user = g.current_user
        defaults = get_route_defaults() or {}
        return jsonify({
            "user": {
                "id": int(user["id"]),
                "email": user.get("email") or "",
                "display_name": user.get("display_name") or "",
                "role": user.get("role") or "",
            },
            "scopes": sorted(g.api_scopes),
            "token": {
                "client_id": g.api_token.get("client_id") or "",
                "expires_at": int(g.api_token.get("expires_at") or 0),
                "resource": g.api_token.get("resource") or "",
            },
            "customer_scope": "all" if territory_user_id() is None else "own_and_unassigned",
            "today_nz": _iso(nz_today()),
            "working_hours": {
                "rep_start": defaults.get("rep_start"),
                "rep_end": defaults.get("rep_end"),
                "customer_open_default": defaults.get("customer_open"),
                "customer_close_default": defaults.get("customer_close"),
            },
            "nexus_url": issuer_url(),
        })

    @bp.route("/customers", methods=["GET"], endpoint="customers")
    def customers():
        args = request.args
        q = _key(args.get("q"))
        tiers = {t.strip().upper() for t in str(args.get("tiers") or "").split(",") if t.strip()}
        group = _key(args.get("group"))
        city = _key(args.get("city"))
        due_within = _int(args.get("due_within_days"))
        near_lat, near_lng = _float(args.get("near_lat")), _float(args.get("near_lng"))
        radius_km = _float(args.get("radius_km"))
        ids = {i for i in (_int(x) for x in str(args.get("ids") or "").split(",")) if i}
        limit = _int(args.get("limit"), 200, 1, 1000)
        offset = _int(args.get("offset"), 0, 0)
        today = nz_today()

        from core.routing import haversine_km

        with db_connect() as connection:
            rows = visible_customer_rows(connection)
            notify_by_id, notify_by_name = notify_contact_index(connection)
            latest_visits = latest_customer_visit_dates(connection, [int(r["id"]) for r in rows]) if rows else {}

        results = []
        for row in rows:
            if ids and int(row["id"]) not in ids:
                continue
            if q and q not in _key(row.get("name")) and q not in _key(row.get("city")) and q not in _key(row.get("street")):
                continue
            if tiers and (row.get("priority_tier") or "C").strip().upper() not in tiers:
                continue
            if group and group not in _key(row.get("customer_group")):
                continue
            if city and city != _key(row.get("city")):
                continue
            notify = notify_by_id.get(int(row["id"])) or notify_by_name.get(_key(row.get("name"))) or []
            item = customer_summary(row, today, notify, latest_visits.get(int(row["id"])))
            if due_within is not None:
                if item["next_due"] is not None and (date.fromisoformat(item["next_due"]) - today).days > due_within:
                    continue
            if near_lat is not None and near_lng is not None and radius_km:
                if item["lat"] is None or item["lng"] is None:
                    continue
                distance = haversine_km(near_lat, near_lng, item["lat"], item["lng"])
                if distance > radius_km:
                    continue
                item["straight_line_km"] = round(distance, 1)
            results.append(item)

        total = len(results)
        return jsonify({
            "total": total,
            "offset": offset,
            "limit": limit,
            "customers": results[offset:offset + limit],
            "notes": "next_due = last_visit + call_interval_days; days_overdue > 0 means overdue. "
                     "cycle_status follows the Nexus customer list (overdue / due within 7 days / not due). "
                     "Customers never visited have next_due null, count as overdue, and are always included by due_within_days.",
        })

    @bp.route("/customers/<int:customer_id>", methods=["GET"], endpoint="customer_detail")
    def customer_detail(customer_id):
        today = nz_today()
        with db_connect() as connection:
            row = visible_customer(connection, customer_id)
            if not row:
                return _error(404, "not_found", "Customer not found, or not in your territory.")
            name = row.get("name") or ""
            connection.row_factory = sqlite3.Row

            clause, params = customer_link_clause(customer_id, name)
            contacts = [dict(r) for r in connection.execute(
                f"SELECT * FROM customer_contacts WHERE {clause} ORDER BY lower(contact_name)", params
            ).fetchall()]

            visits = []
            try:
                for r in connection.execute(
                    f"SELECT * FROM road_visit_notes WHERE {clause} AND (completed=1 OR visit_notes<>'') "
                    "ORDER BY visit_date DESC, visit_time DESC LIMIT 10", params
                ).fetchall():
                    r = dict(r)
                    visits.append({
                        "date": r.get("visit_date"), "time": r.get("visit_time") or "",
                        "source": "nexus", "completed": bool(int(r.get("completed") or 0)),
                        "skipped": bool(int(r.get("skipped") or 0)),
                        "outcome": r.get("outcome") or "", "notes": r.get("visit_notes") or "",
                        "next_step": r.get("next_step") or "", "appointment_id": r.get("appointment_id") or "",
                    })
            except Exception:
                pass
            try:
                for r in connection.execute(
                    f"SELECT * FROM historical_customer_calls WHERE {clause} ORDER BY visit_date DESC LIMIT 10", params
                ).fetchall():
                    r = dict(r)
                    visits.append({
                        "date": r.get("visit_date"), "time": r.get("start_time") or "",
                        "source": "history_import", "completed": True, "skipped": False,
                        "outcome": r.get("subject") or "", "notes": r.get("visit_notes") or "",
                        "next_step": r.get("actions_text") or "", "appointment_id": "",
                    })
            except Exception:
                pass
            visits.sort(key=lambda v: (str(v["date"] or ""), str(v["time"] or "")), reverse=True)

            action_rows = [dict(r) for r in connection.execute(
                f"SELECT * FROM store_actions WHERE {clause} ORDER BY due_date, id", params
            ).fetchall()]
            latest = latest_customer_visit_dates(connection, [customer_id]).get(customer_id)
            requirement = None
            try:
                requirement = active_next_visit_requirement(connection, customer_id=customer_id, store_name=name)
            except Exception:
                requirement = None

        own_actions = private_workspace_user_id()
        actions = [
            action_json(a) for a in action_rows
            if _key(a.get("status")) not in DONE_ACTION_STATUSES
            and (own_actions is None or a.get("owner_user_id") in (None, own_actions))
        ]
        notify = [c for c in contacts if int(c.get("notify_before_visit") or 0)]
        detail = customer_summary(row, today, notify, latest)
        detail.update({
            "call_note": row.get("call_note") or "",
            "contacts": [{
                "contact_id": int(c["id"]),
                "name": c.get("contact_name") or "",
                "role": c.get("role") or "",
                "phone": c.get("phone") or "",
                "email": c.get("email") or "",
                "notes": c.get("notes") or "",
                "notify_before_visit": bool(int(c.get("notify_before_visit") or 0)),
                "preferred_shedbuilder_contact": bool(int(c.get("preferred_shedbuilder_contact") or 0)),
            } for c in contacts],
            "recent_visits": visits[:10],
            "open_actions": actions,
            "next_visit_requirement": requirement,
        })
        return jsonify(detail)

    @bp.route("/start-locations", methods=["GET"], endpoint="start_locations")
    def start_locations():
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(r) for r in connection.execute(
                "SELECT * FROM route_start_locations WHERE owner_user_id=? ORDER BY is_home DESC, lower(name), id",
                (int(g.current_user["id"]),),
            ).fetchall()]
        return jsonify({"locations": [{
            "location_id": int(r["id"]),
            "name": r.get("name") or "",
            "label": r.get("label") or "",
            "lat": _float(r.get("latitude")),
            "lng": _float(r.get("longitude")),
            "is_home": bool(int(r.get("is_home") or 0)),
        } for r in rows]})

    @bp.route("/drive-times", methods=["POST"], endpoint="drive_times")
    def drive_times():
        payload = request.get_json(silent=True) or {}
        customer_ids = [i for i in (_int(x) for x in payload.get("customer_ids") or []) if i]
        location_ids = [i for i in (_int(x) for x in payload.get("location_ids") or []) if i]
        raw_points = payload.get("points") or []
        if len(customer_ids) + len(location_ids) + len(raw_points) > MAX_POINTS:
            return _error(400, "too_many_points", f"Ask for at most {MAX_POINTS} points at a time.")

        points, missing = [], []
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            for location_id in location_ids:
                r = connection.execute(
                    "SELECT * FROM route_start_locations WHERE id=? AND owner_user_id=?",
                    (location_id, int(g.current_user["id"])),
                ).fetchone()
                if not r:
                    missing.append(f"location:{location_id}")
                    continue
                r = dict(r)
                points.append({"key": f"location:{location_id}", "label": r.get("name") or "",
                               "lat": _float(r.get("latitude")), "lng": _float(r.get("longitude"))})
            for customer_id in customer_ids:
                r = visible_customer(connection, customer_id)
                if not r:
                    missing.append(f"customer:{customer_id}")
                    continue
                points.append({"key": f"customer:{customer_id}", "label": r.get("name") or "",
                               "lat": _float(r.get("latitude")), "lng": _float(r.get("longitude"))})
        for index, p in enumerate(raw_points):
            lat, lng = _float((p or {}).get("lat")), _float((p or {}).get("lng"))
            if lat is None or lng is None:
                missing.append(f"point:{index}")
                continue
            points.append({"key": str((p or {}).get("key") or f"point:{index}"), "label": str((p or {}).get("label") or ""),
                           "lat": lat, "lng": lng})
        if missing:
            return _error(404, "not_found", "Not found or not visible: " + ", ".join(missing))
        if len(points) < 2:
            return _error(400, "too_few_points", "Give at least two points.")

        result = road_matrix(points)
        audit_event("api_drive_times", f"{len(points)} points, source={result['source']}")
        return jsonify({
            "source": result["source"],
            "source_detail": result["detail"],
            "warning": None if result["source"] == "road" else
                "Some or all drive times are straight-line estimates, not real road times.",
            "points": points,
            "minutes": [[round(v, 1) for v in row] for row in result["minutes"]],
            "km": [[round(v, 1) for v in row] for row in result["km"]],
        })

    @bp.route("/trips", methods=["GET"], endpoint="trips")
    def trips():
        start = _parse_day(request.args.get("from")) or (nz_today() - timedelta(days=30))
        end = _parse_day(request.args.get("to")) or (nz_today() + timedelta(days=365))
        owner = trip_owner_user_id()
        if owner is None and is_admin():
            owner = scope_owner_filter()
        where, params = ["end_date>=?", "start_date<=?"], [start.isoformat(), end.isoformat()]
        if owner is not None:
            where.append("owner_user_id=?")
            params.append(owner)
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(r) for r in connection.execute(
                f"SELECT * FROM trip_plans WHERE {' AND '.join(where)} ORDER BY start_date, id", params
            ).fetchall()]
            counts = {}
            if rows:
                marks = ",".join("?" for _ in rows)
                for r in connection.execute(
                    f"SELECT trip_id, COUNT(*) FROM trip_appointments WHERE trip_id IN ({marks}) GROUP BY trip_id",
                    [int(r["id"]) for r in rows],
                ).fetchall():
                    counts[int(r[0])] = int(r[1])
        return jsonify({"from": start.isoformat(), "to": end.isoformat(), "trips": [trip_json(r, counts.get(int(r["id"]), 0)) for r in rows]})

    def trip_json(r, appointment_count=None):
        out = {
            "trip_id": int(r["id"]),
            "name": r.get("trip_name") or "",
            "destination": r.get("destination") or "",
            "start_date": r.get("start_date"),
            "end_date": r.get("end_date"),
            "travel_method": r.get("travel_method") or "",
            "flight_status": r.get("flight_status") or "",
            "rental_status": r.get("rental_status") or "",
            "accommodation_status": r.get("accommodation_status") or "",
            "appointments_confirmed": bool(int(r.get("appointments_confirmed") or 0)),
            "completed": bool(r.get("completed_at")),
            "owner_user_id": _int(r.get("owner_user_id")),
            "notes": r.get("notes") or "",
        }
        if appointment_count is not None:
            out["appointment_count"] = appointment_count
        return out

    @bp.route("/trips/<int:trip_id>", methods=["GET"], endpoint="trip_detail")
    def trip_detail(trip_id):
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            trip = trip_row_for_current_user(connection, trip_id)
            if not trip:
                return _error(404, "not_found", "Trip not found, or not yours.")
            trip = dict(trip)
            schedule = latest_schedule(connection, trip_id)
            name_to_id = {_key(r[1]): int(r[0]) for r in connection.execute("SELECT id,name FROM route_customers").fetchall()}
            appointments = []
            if schedule:
                appointments = parse_schedule_appointments(
                    schedule.get("raw_html") or "", from_date=date(1900, 1, 1), canonical_start_date=trip.get("start_date")
                )
            uid_pairs = []
            for a in appointments:
                uid = appointment_uid(trip_id, schedule["id"], a["date"].isoformat(), a["time"], a["store"])
                source_date = a.get("source_date") or a["date"]
                source_uid = appointment_uid(trip_id, schedule["id"], source_date.isoformat(), a["time"], a["store"])
                uid_pairs.append((uid, source_uid))
            notes_by_uid = {}
            all_uids = sorted({u for pair in uid_pairs for u in pair})
            for start in range(0, len(all_uids), 200):
                chunk = all_uids[start:start + 200]
                marks = ",".join("?" for _ in chunk)
                for r in connection.execute(
                    f"SELECT * FROM road_visit_notes WHERE appointment_id IN ({marks})", chunk
                ).fetchall():
                    notes_by_uid[dict(r).get("appointment_id")] = dict(r)

        days = {}
        for a, (uid, source_uid) in zip(appointments, uid_pairs):
            note = notes_by_uid.get(uid) or notes_by_uid.get(source_uid) or {}
            customer_id = a.get("customer_id") or name_to_id.get(_key(a["store"]))
            days.setdefault(a["date"].isoformat(), []).append({
                "appointment_id": uid,
                "time": a.get("time") or "",
                "customer_id": customer_id or None,
                "store_name": a["store"],
                "duration_minutes": a.get("duration") or _duration_from_note(a.get("call_note")),
                "appointment_type": KIND_NAMES.get(a.get("appointment_kind") or "", a.get("appointment_kind") or "flexible"),
                "preferred_time": a.get("preferred_time") or "",
                "call_note": a.get("call_note") or "",
                "timing_warning": a.get("timing_warning") or "",
                "completed": bool(int(note.get("completed") or 0)),
                "skipped": bool(int(note.get("skipped") or 0)),
                "outcome": note.get("outcome") or "",
                "next_step": note.get("next_step") or "",
                "has_notes": bool(note.get("visit_notes")),
            })
        detail = trip_json(trip)
        detail.update({
            "schedule_id": int(schedule["id"]) if schedule else None,
            "has_schedule": bool(schedule),
            "days": [{"date": d, "stops": days[d]} for d in sorted(days)],
        })
        return jsonify(detail)

    def action_json(a):
        return {
            "action_id": int(a["id"]),
            "customer_id": _int(a.get("customer_id")),
            "store_name": a.get("store_name") or "",
            "title": a.get("title") or "",
            "detail": a.get("detail") or "",
            "priority": a.get("priority") or "",
            "status": a.get("status") or "",
            "due_date": a.get("due_date") or "",
            "owner_user_id": _int(a.get("owner_user_id")),
            "source": a.get("source") or "",
            "updated_at": a.get("updated_at") or "",
        }

    @bp.route("/actions", methods=["GET"], endpoint="actions")
    def actions():
        status = str(request.args.get("status") or "open").strip().lower()
        customer_id = _int(request.args.get("customer_id"))
        limit = _int(request.args.get("limit"), 200, 1, 1000)
        with db_connect() as connection:
            connection.row_factory = sqlite3.Row
            where, params = [], []
            if customer_id:
                customer = visible_customer(connection, customer_id)
                if not customer:
                    return _error(404, "not_found", "Customer not found, or not in your territory.")
                clause, clause_params = customer_link_clause(customer_id, customer.get("name") or "")
                where.append(clause)
                params.extend(clause_params)
            own = private_workspace_user_id()
            if own is not None:
                where.append("(owner_user_id=? OR owner_user_id IS NULL)")
                params.append(own)
            elif is_admin() and scope_owner_filter() is not None:
                where.append("owner_user_id=?")
                params.append(scope_owner_filter())
            sql = "SELECT * FROM store_actions" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY due_date, id"
            rows = [dict(r) for r in connection.execute(sql, params).fetchall()]
            visible_names = None
            if territory_user_id() is not None:
                visible_names = {_key(r.get("name")) for r in visible_customer_rows(connection)}
        if status == "open":
            rows = [r for r in rows if _key(r.get("status")) not in DONE_ACTION_STATUSES]
        if visible_names is not None:
            rows = [r for r in rows if _key(r.get("store_name")) in visible_names]
        return jsonify({"total": len(rows), "actions": [action_json(r) for r in rows[:limit]]})

    app.register_blueprint(bp)
    return bp
