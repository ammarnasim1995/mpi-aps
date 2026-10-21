# MPI-APS — Midas Production Intelligence & Advanced Planning System (v0.2, working prototype)

Browser-based production planning for **knitting → dipping → packing** across multiple plants.
Python 3.11+ (tested on 3.13), **standard library only** (no pip dependencies), SQLite persistence, vanilla-JS UI.

> **Data status:** every record shipped with this build is **SYNTHETIC SAMPLE DATA** (`origin=sample`), including rates,
> efficiency factors, cure times, changeovers and stock. It is **not** Midas data and **not** SAP data. The UI banner says so.
> No SAP connection exists yet. Uploaded files (orders CSV) are tagged `upload`.

## Quick start (local demo)
```bash
cd mpi-aps-core
MPI_DEV_AUTH=1 python3 -m mpi_aps          # http://127.0.0.1:8080  (demo users, loopback only)
PYTHONPATH=. python3 -m unittest discover -s tests -t . -v      # run the tests
```
**Loading your real data:** sign in as `demo.admin` → *Master Data & Validation* → *Import / export*. Download the template (or export the sample) for each entity, fill it, import. Load plants → resources → SKUs → routings → versions → rates → stock/BOMs → changeovers; then review *Findings* (cross-entity problems are reported, never repaired). Efficiency factors and cure rules are set on *Planning Settings*. Orders import is on *Demand & Orders*. Data origin is tagged by upload channel.

Sign in with a demo user (e.g. `demo.admin`, `demo.central`, `demo.central2`, `demo.pk_planner`, `demo.management`).
`MPI_DEV_AUTH=1` is refused unless `MPI_HOST` is loopback.

## Configuration (environment variables; nothing secret in source control)
| Variable | Default | Meaning |
|---|---|---|
| `MPI_HOST` / `MPI_PORT` | `127.0.0.1` / `8080` | Bind address |
| `MPI_DB_PATH` | `mpi_aps.sqlite` | SQLite file (migrations run automatically at start) |
| `MPI_USERS_FILE` | – | JSON list of users with SHA-256 token hashes (production auth), kept **outside** the repo |
| `MPI_DEV_AUTH` | `0` | `1` = demo users via `X-Demo-User` header (loopback only) |
| `MPI_PLAN_DATE` | today | Planning start date (horizon = 12 weeks) |
| `MPI_FOUR_EYES` | `1` | Approver must differ from plan author |
| `MPI_LOG_LEVEL` | `INFO` | JSON structured logs to stdout |

**Create a production user:** `python3 -m mpi_aps.auth create-user alice central_planner "*"` prints the JSON entry (add it to the
users file) and the one-time token on stderr. Roles: `central_planning_admin, central_planner, plant_planner, production_supervisor,
procurement_user, management_viewer` (see `mpi_aps/auth.py` for the permission matrix). Terminate TLS in a reverse proxy; the app
speaks plain HTTP.

## What is implemented vs pending
| Area | Status |
|---|---|
| Units (pairs/dozens, material-specific UoM), gross/net rates, efficiency hierarchy, cure time, freshness (`mpi_aps_core`) | **Implemented, tested** |
| Demand: order import (CSV) with validation, duplicate prevention, reconciliation in pairs; priority rules + overrides with reason | **Implemented, tested** |
| Planning engine: net requirements (FG/WIP/yield), lots/splits, material ledger (delay/block rules), candidate selection (recipe × resource, gauge/bed), finite-capacity forward scheduling with shift calendars, downtime, changeovers, precedence, cure/quality | **Implemented, tested** (heuristic, see limits) |
| Deterministic `resolve` recomputation (downstream dates after any change), constraint re-validation of manual moves, undo | **Implemented, tested** |
| Capacity: gross/planned/unplanned downtime/net/overtime/scheduled/remaining, utilisation, rough-cut overload, bottlenecks | **Implemented, tested** |
| Scenarios (overtime, breakdown, maintenance, rate change, demand, material delay, plant move, expedite, lot split), comparison vs baseline, breakdown impact | **Implemented, tested** |
| Draft → Approved → Published lifecycle, immutability, four-eyes, audit trail, stale-data acknowledgment | **Implemented, tested** |
| Data-quality validation + SKU→resource mapping matrix + SAP-vs-demonstrated rate comparison | **Implemented, tested** |
| RBAC + plant scoping (server-side), structured logging, input validation, migrations | **Implemented, tested** |
| UI: 15 screens incl. Gantt with drag-and-drop | **Implemented**; browser-verified (see below) |
| **Master-data CSV import / export** (plants+calendars, resources, downtimes, SKUs, routings, production versions, rates, FG/WIP stock, raw stock, receipts, BOMs, changeover rules, material rules): per-entity complete-snapshot replace, strict all-or-nothing by default, row validation, duplicate-key rejection, row-count reconciliation, persisted, audited, revert-to-sample, templates and export of current data | **Implemented, tested** (Master Data → Import / export) |
| Row-by-row **editing screens** for master data, resource-specific calendar exceptions | **Planned** — change data by re-importing the entity snapshot |
| **SAP adapters** (HANA views/RFC/OData), scheduled refresh | **Planned** — interface and mapping proposal in `docs/SAP_INTEGRATION.md`; nothing connected |
| **Execution & performance** (actuals, plan adherence, downtime, rejection) | **Planned** — screen states this explicitly; no numbers shown |
| Backward scheduling / latest-start slack; allocation rules FIFO/FEFO; weighted-objective UI; saved filters; bulk assignment; scenario→draft promotion; AI narration | **Planned** |
| PostgreSQL, multi-process deployment, SSO | **Planned** (single process + global lock today) |

## Verification actually performed
* `PYTHONPATH=. python3 -W error::ResourceWarning -m unittest discover -s tests -t .` → **75 tests, OK** (28 core + 47 application:
  calendar, requirements, scheduling, 15 acceptance scenarios, data quality, master-data import/export round-trip, RBAC, HTTP boundary).
* Headless Chromium: all 15 screens render without script errors; Gantt drag-and-drop exercised (valid move applied and downstream
  recalculated; invalid moves rejected with reasons; all audited). Found and fixed two UI defects this way.
* Not verified: behaviour on real Midas data, real SAP data, PostgreSQL, concurrent multi-user load, browsers other than Chromium.

## Known limitations (read before relying on results)
* Scheduler is a **transparent greedy heuristic** (priority-ordered level 0, ready-time-ordered downstream; earliest-finish with weighted
  score). It is not an optimiser, does not backfill gaps or look ahead, and may be beaten by manual sequencing.
* Lot-for-lot flow: dipping starts after the **whole** knitting lot finishes (+transfer/queue); use lot splits for overlap.
* Rough-cut overload is optimistic (infinite capacity, fastest resource, due-week buckets, ignores downstream lead time).
* All datetimes are naive plant-local; no time-zone handling. Plan "now" is `MPI_PLAN_DATE` 00:00.
* Plans store decisions + a snapshot; master/inputs are **not** versioned, so re-opening an old plan re-resolves against current inputs.
  (Approved/published plans display their stored snapshot via `/api/plans/{id}`.)
* Changeover times, cure times, efficiency factors and rates in the sample are invented placeholders.

## Troubleshooting
| Symptom | Cause / action |
|---|---|
| Server exits "No authentication configured" | Set `MPI_USERS_FILE` (production) or `MPI_DEV_AUTH=1` (local demo) |
| 401 in UI | Token wrong/missing; dev mode needs a demo user on loopback |
| Order shows *Unscheduled* | Read the reason: missing rate/recipe/cure rule, unit mismatch, material shortage with `block` rule. Fix the data (Master Data screen lists affected records); nothing is auto-repaired |
| Banner "Inputs changed since this plan was generated" | Orders/settings changed; click through to *Plans* and generate a new draft |
| Publish refused: critical data stale | Refresh the source, or an authorised user acknowledges it on *Data Refresh* (audited), where policy permits |
| Move rejected `EARLIEST_FEASIBLE` | The engine reports the earliest feasible start and the limiting constraint |
| `sqlite3.OperationalError: database is locked` | Another process holds the DB; run one server per DB file |
| Reset demo data | Stop the server and delete the SQLite file |

See `docs/` for architecture, calculations, data dictionary, SAP integration plan and the acceptance matrix.
