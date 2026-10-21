# Architecture
```
Browser (static/ vanilla JS)  ──HTTP/JSON──▶  api.py  (routing, authN/Z, validation, logging)
                                                 │
                                           service.py (orchestration, audit, permissions, plan lifecycle)
                                   ┌─────────────┼──────────────┬───────────────┐
                              planner.py     analytics.py   validation.py   importer.py     ← deterministic, no I/O
                                   └── mpi_aps_core (units, rates, settings, curing, freshness) ──┘
                                                 │
                                            store.py (SQLite; PostgreSQL target)      auth.py
```
Separation of concerns: UI (`static/`), API (`api.py`), calculations (`planner.py`, `analytics.py`, `mpi_aps_core`), ingestion
(`importer.py`; SAP adapters planned), persistence (`store.py`), auth (`auth.py`), AI explanations (not connected; engine produces
all narratives; see `analytics.explain_order`). **All numbers come from the engine; the UI performs no planning math.**

## Schedule model
A schedule = **decisions** (op → resource/recipe, per-resource sequence, optional not-before pin, manual author/reason) +
**derived times**. `planner.resolve()` recomputes every start/finish in topological order over precedence edges and per-resource
sequence edges; a cycle raises `ScheduleCycle`. Start of an operation = max of: plan start, predecessor output (+transfer+queue; for
packing: cure/quality release), material-ready (knitting), manual pin, previous op on the resource; then working time is consumed
across shift calendars minus downtime. `driver` records which component was binding (used for explanations and limiting chains).

Auto-scheduler (`planner.generate`): orders in priority order → lots → ops. Level 0 (knitting) by priority; later levels by ready
date then priority. Per op, every eligible (recipe, resource) is trial-placed; score = 1.0·finish_h + 0.5·setup_h + 0.1·resource_load_h
+ 2.0·overtime_h (weights in `PlannerConfig.objective_weights`); lowest wins, ties by resource id. Hard constraints are
filters/blocks, never scored: eligibility (production version + rate + gauge/bed), valid rate, calendars/downtime, material rule,
precedence, cure + quality, WIP supply. `validate_schedule` independently re-checks eligibility, material, precedence, cure/quality,
packing supply and overlaps — used for every manual override.

## Database (SQLite, migrations in `store.MIGRATIONS`, tracked in `schema_migrations`)
`plans(id,name,kind plan|scenario,status draft|approved|published|superseded,base_plan_id,created_by,created_at,decisions,snapshot,meta,approved_*,published_*)` ·
`audit(ts,user,action,entity,previous,new,reason,approval_status)` · `orders(so,line,payload,origin,import_id)` ·
`refresh_log(source,origin,attempted_at,success,file_name,rows,accepted,rejected,report,errors,user)` · `overrides(order_key,rank,reason,user,ts)` ·
`acknowledgments` · `kv` (settings operations log, stale-data acks, undo stacks). **Migrations:** append `(version, sql)` to `MIGRATIONS`;
they apply once, in order, at startup; never edit an applied migration. PostgreSQL: SQL is portable apart from `AUTOINCREMENT` (use identity columns).

## API (JSON; `Authorization: Bearer <token>`; all routes server-side permission-checked)
| Method & path | Permission | Purpose |
|---|---|---|
| GET `/api/meta` `/dashboard` `/orders` `/orders/{key}/explain` `/planning/{process}` `/gantt` `/capacity` `/exceptions` `/material` `/validation` `/refresh` `/settings` `/plans` `/plans/{id}` `/scenarios` | view | Read models; filters: plant, country, process, family, customer, sku, order, resource, priority, date_from/to, status, plan_id |
| GET `/api/audit` | view_audit | Audit trail |
| POST `/api/plans/generate` · `/plans/{id}/clone` · `/move` · `/undo` | edit_schedule | Draft lifecycle; move needs `op, resource, reason` (+`not_before`) |
| POST `/api/plans/{id}/approve` · `/publish` | approve · publish | Four-eyes; publish blocked on unacknowledged stale critical data |
| POST `/api/orders/import` · `/orders/{key}/priority` (DELETE clears) | import · edit_schedule | CSV `{csv,file_name,control_rows,control_qty(pairs),replace_sample}`; override needs `rank, reason` |
| POST `/api/scenarios/run` · `/scenarios/breakdown` | run_scenarios | Simulation on a copy |
| POST `/api/refresh/acknowledge` | ack_stale | Audited continue-on-stale |
| POST `/api/settings/{efficiency|cure|freshness}` | settings | Needs `payload, reason`; audited; persisted |
Errors: `{"error":{"code","message"}}` with 400/401/403/404/409/413/500.

## Security controls present / missing
Present: server-side RBAC and plant scoping, hashed tokens, dev auth limited to loopback, 5 MB body limit, JSON-only bodies, HTML escaping in UI,
CSP + nosniff headers, static path-traversal guard, no secrets in repo (`.gitignore` covers `.env`, DBs), parameterised SQL, audit of all changes.
Missing/planned: TLS (use reverse proxy), SSO/OIDC, token expiry/rotation, rate limiting, CSRF is N/A for bearer tokens but cookie sessions would need it,
DB encryption/backups, PostgreSQL row-level controls.
