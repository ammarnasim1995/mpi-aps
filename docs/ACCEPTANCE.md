# Acceptance matrix (all automated; run: `PYTHONPATH=. python3 -m unittest discover -s tests -t . -v`)
| # | Scenario | Test(s) | Result at last run |
|---|---|---|---|
| 1 | Imported order → correct open qty | `A01_Import.test_open_quantity_and_rejections` | pass |
| 2 | Multi-machine SKU assigned to compatible resource | `A02_A03_Eligibility_Rates.test_multiple_machines_and_assignment`, `test_ineligible_resource_rejected_not_substituted` | pass |
| 3 | Resource-specific rates → different, correct hours | `test_resource_specific_rates_give_different_hours` | pass |
| 4 | Insufficient hours → overloaded flagged | `A04_Overload.test_overloaded_resource_flagged` | pass |
| 5 | Dipping cannot use knit output before available | `A05_A06_A07_Precedence.test_dipping_cannot_start_before_knitting_output` | pass |
| 6 | Packing ≤ dipped WIP, cure + quality respected | `test_packing_respects_cure_quality_and_supply`, `test_missing_cure_rule_blocks_packing` | pass |
| 7 | Knitting change recalculates dipping/packing | `test_knitting_change_recalculates_downstream` | pass |
| 8 | Breakdown → impact analysis, affected orders | `A08_Breakdown.test_breakdown_identifies_affected_orders` | pass |
| 9 | Material shortage prevents/delays per rule | `A09_Material.test_block_rule_prevents_and_delay_rule_delays` | pass |
| 10 | Scenario vs approved baseline | `A10_A14.test_scenario_compared_and_approved_plan_untouched` | pass |
| 11 | Overrides validated + audited | `test_manual_override_validated_and_audited`; browser drag test | pass |
| 12 | Import reconciled, rejects reported | `A11_A12_A13_A15.test_import_reconciliation_reports_differences` | pass |
| 13 | actual/estimated/sample/live distinguished | `test_data_origin_distinguished` (UI banner + freshness labels) | pass (live/SAP N/A: not connected; actuals not available) |
| 14 | Approved plan not silently overwritten | `test_approved_plan_immutable_and_four_eyes`, scenario test | pass |
| 15 | Recommendations explained | `test_every_op_has_explanation_and_late_orders_have_alternatives` | pass |
Additional: RBAC/plant scope (`test_plant_scope_enforced`, `test_api.Http`), data-quality checks, calendar maths, yield/FG/WIP netting, lots, changeover.
