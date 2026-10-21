# Calculation definitions (all deterministic; Decimal arithmetic; hours rounded half-up to 4 dp at the end)
| Item | Definition |
|---|---|
| Open quantity (pairs) | `max(0, ordered − delivered)` converted to PR with the material-specific factor (1 DZ = 12 PR unless the material says otherwise; other units need an explicit factor, else error). Only `status=open` orders. |
| Net requirement | Walk the route backwards: at each stage `use = min(out, available stock of that stage)` (FG at packing, DIP_WIP at dipping, KNIT_WIP at knitting; available = qty − allocated − quality-blocked; allocated to orders in priority order), `out −= use`, `run = ceil(out / yield)`, upstream `out = run`. Yield is separate from rate. |
| Required hours | `qty_in_rate_unit ÷ effective_rate`. Net rate: effective = rate. Gross: effective = rate × efficiency factor. Basis unknown / unvalidated / rejected / missing: blocked, never defaulted. |
| Efficiency factor | Hierarchy resource → plant+process → plant → approved system default; only approved, in-effect entries; overlap at one level = conflict. |
| Setup / changeover | Added separately before run, from the previous op on that resource: knitting {sku, family, gauge}, dipping {sku, family, coating}, packing {sku, family}. Not added when the rate `includes_setup`. Values are sample assumptions. |
| Packing earliest start | `max(dip_finish + min_cure + additional_processing, dip_finish + qa_checkpoint) + dip.transfer + pack.queue`. Missing/conflicting cure rule ⇒ blocked. Quality status of new output is assumed released after the QA checkpoint (existing blocked stock is excluded). |
| Material readiness | Knitting need = run_qty × BOM qty/pair; allocated in priority order against `stock − reserved − blocked`; `delay` rule waits for the first receipt that covers cumulative need, `block` rule prevents scheduling; BOM/stock unit mismatch ⇒ blocked. |
| Gross available hours | Standard shift hours on working, non-holiday days |
| Planned downtime | Maintenance overlapping shifts; unplanned = breakdowns |
| Net available hours | gross − planned − unplanned; approved overtime reported separately (`overtime_available`) |
| Scheduled hours | Segment hours (setup+run) clipped to the bucket |
| Utilisation % | `scheduled ÷ (net available + overtime available) × 100` |
| Remaining capacity | `max(0, net + overtime − scheduled)` |
| Rough-cut overload | Per resource: cumulative fastest-eligible-resource demand by due week vs cumulative net capacity; overloaded if shortage > 0.5 h |
| Due-date feasibility | `slack = end-of-due-day − (last finish + shipment buffer)`; late < 0; at risk < `at_risk_days` (2); unscheduled if any op blocked |
| Bottleneck | Ranked by (late/at-risk orders whose limiting chain passes a "resource busy" step on it, utilisation to busy-until) |
| Scenario comparison | Full engine run on a deep copy; KPI deltas, hours by process, per-order completion/status change |
