# Riverlea OS module map

This repository is intentionally split by business area so production Sales Assistant fixes and Riverlea OS development can be carried between branches with minimal overlap.

## Entry point

- `app.py` — Flask startup, environment/session setup, remaining legacy/shared orchestration, and module registration. New feature logic should not be added here unless it is genuinely cross-cutting.

## Shared core

- `core/db.py` — SQLite/Neon Postgres compatibility, database connection helpers, database paths and database error types.
- `core/time_utils.py` — New Zealand timezone/date helpers and the `date_nz` template formatter.
- `core/__init__.py` — stable imports exposed to the rest of the app.

## Riverlea OS modules

- `home/` — Home / Control Centre.
- `actions/` — unified Actions Hub and meeting To-Dos.
- `meetings/` — Sales Meeting workflow and meeting orchestration.
- `scorecard/` — weekly KPI grid, overrides, notes and exemptions.
- `rocks/` — quarterly Rocks and milestones.
- `issues/` — meeting discussion queue and Issue lifecycle.
- `qc/` — QC capture and future NetSuite case integration boundary.

## Sales modules

- `customers/` — customer list/detail, contacts and customer-level helpers.
- `visits/` — On the Road / visit execution.
- `trips/` — Trips, Trip Workspace, calendar and travel expenses.
- `route_planner/` — route planning and route-to-trip workflow.
- `reporting/` — Sales Performance, saved reporting periods and bonus reporting.

## Platform modules

- `nexus_api/` — Claude/MCP integration: OAuth sign-in (`oauth.py`) and the read-only bearer-token JSON API at `/api/v1` (`routes.py`). Used by the separate `nexus-mcp` service.
- `admin/` — users, territories, settings, data/recovery and health tools.
- `auth/` — login/logout, session loading and role decorators.

## Where to make future changes

| Change | Primary module |
| --- | --- |
| Customer page/contact/priority | `customers/` |
| Visit notes, outcomes, training, road workflow | `visits/` |
| Trip workspace/calendar/travel expenses | `trips/` |
| Route building/planning | `route_planner/` |
| Action/To-Do behavior | `actions/` |
| Meeting flow/ratings/agenda | `meetings/` |
| KPI/Scorecard behavior | `scorecard/` |
| Rocks/milestones | `rocks/` |
| Meeting Issues | `issues/` |
| QC capture/NetSuite case sync | `qc/` |
| Home dashboard | `home/` |
| Sales reporting | `reporting/` |
| Users/territories/settings/data | `admin/` |
| Login/permissions | `auth/` |
| Database adapter/timezone helpers | `core/` |
| Drive-time matrix / routing server | `core/routing.py` |
| Claude/MCP API and OAuth | `nexus_api/` |

## Branch workflow

- `main` remains the live Sales Assistant.
- `staging` remains Riverlea OS development.
- Production fixes should be made on `main`, then carried into the corresponding module on `staging`.
- Avoid implementing the same fix independently in both branches.
- Do not merge Riverlea OS feature work back to `main` until the OS is ready for production.

## Design rule

Keep new feature logic in its owning module. Prefer explicit dependencies between modules over copying helpers or constants into multiple places.
