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

## Master-data CSV formats (`GET /api/templates/{entity}.csv`, `GET /api/master/{entity}/export.csv`)
Entities: plants, resources, downtimes, skus, routings, versions, rates, stocks, raw_stocks, receipts, boms, changeovers, material_rules.
Conventions: UTF-8 CSV with header; dates ISO `yyyy-mm-dd`, datetimes ISO `yyyy-mm-ddThh:mm` (naive plant-local); decimals with `.`; booleans `Y/N`;
blank optional cells = none. `plants.shifts` = `6-14;14-22`; `working_weekdays` = `0,1,2,3,4,5` (Mon=0); `holidays` = `;`-separated dates.
`rates`: `kind` = sap_standard|demonstrated|planned; `basis` = gross|net|unknown; `validation_status` = validated|unvalidated|rejected; `unit` e.g. `dozens/machine-hour`.
Key columns (duplicates rejected): plants[plant]; resources[resource]; downtimes[resource,start]; skus[sku]; routings[sku,plant,seq]; versions[sku,plant,process,recipe,valid_from];
rates[sku,plant,process,resource,recipe,kind,effective_from]; stocks[sku,plant,stage]; raw_stocks[material,plant]; receipts[material,plant,date]; boms[sku,component]; changeovers[process]; material_rules[material].
