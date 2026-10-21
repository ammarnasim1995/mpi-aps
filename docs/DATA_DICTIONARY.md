# Data dictionary (core entities; source for the prototype = `mpi_aps/sample.py`, SYNTHETIC)
Every record has `origin` ∈ sample | upload | sap_live | estimate | manual. Identifiers are matched **exactly**; similar names are never treated as equal.
| Entity (business key) | Key fields and meaning |
|---|---|
| Plant (`id`) | country, calendar (shifts, working weekdays, holidays, overtime windows) |
| Resource (`id`) | plant, process (knitting/dipping/packing), group, gauge, bed (knitting compatibility), active, calendar exception, downtimes (maintenance/breakdown) |
| SKU (`sku`) | SAP material code (UoM lookups), family, coating, gauge, bed, customer_pack, base UoM (PR) |
| Routing op (`sku, plant|any, seq`) | process, predecessor process, transfer_h (to next), queue_h, yield (good/processed), qa_checkpoint_h |
| Production version (`sku, plant, process, recipe, valid_from`) | recipe id, validity; a recipe is only usable on resources that have a rate for it |
| Production rate (`sku, plant, process, resource, recipe, kind`) | value, unit (e.g. dozens/machine-hour), basis gross/net/unknown, kind SAP_STANDARD/DEMONSTRATED/PLANNED, validation status, source, effective dates, includes_setup |
| Efficiency factor / cure rule / freshness policy | `mpi_aps_core.settings` — scope, value, approval, effective dates, source; audited |
| Changeover rule (`process`) | hours for sku/family/coating/gauge change (sample assumptions) |
| Order (`so, line`) | customer, sku, qty+uom, delivered, confirmed, requested/confirmed date, customer & shipment priority, country, plant, status |
| Stock (`sku, plant, stage`) | FG / DIP_WIP / KNIT_WIP qty (pairs), allocated, quality-blocked |
| Raw stock (`material, plant`) · Receipt · BOM line · Material rule | unit, qty, reserved, blocked · expected receipts · qty per pair + unit · on_shortage delay/block |
| Plan (`id`) | kind plan/scenario, status, decisions, snapshot (KPIs, orders, ops), author/approver/publisher |
| Audit event | user, timestamp, action, entity, previous, new, reason, plan status |
| Refresh record | source, origin, attempted_at, success, file, rows, accepted/rejected, reconciliation report |
Not yet modelled: sales-order schedule lines, production orders/confirmations (actuals), customer-specific packaging requirements, FEFO batches, per-shift calendars by resource group, plant transfers/lead times.
