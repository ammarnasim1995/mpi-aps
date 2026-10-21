import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from mpi_aps.calendar import allocate, hours_breakdown
from mpi_aps.model import CalendarSpec, Downtime
from mpi_aps.planner import Ctx, generate, requirements, validate_schedule
from mpi_aps.sample import build_sample
from mpi_aps import validation as val
from mpi_aps_core.settings import Scope

from .fixtures import NOW, PD, clean, order


class Calendar(unittest.TestCase):
    def test_allocate_spans_shifts_holidays_and_downtime(self):
        c = CalendarSpec([(6, 14), (14, 22)], frozenset(range(6)), frozenset({date(2026, 10, 24)}))
        segs = allocate(c, [Downtime(datetime(2026, 10, 23, 6), datetime(2026, 10, 23, 10), "breakdown")], datetime(2026, 10, 23, 6), 20)
        self.assertEqual(segs[0].start, datetime(2026, 10, 23, 10))                  # skips outage
        self.assertEqual(segs[-1].end, datetime(2026, 10, 26, 14))  # Fri 12h, Sat holiday, Sun off, Mon 06-14 = 8h
        self.assertAlmostEqual(sum(s.hours for s in segs), 20)

    def test_hours_breakdown_definitions(self):
        c = CalendarSpec([(6, 14), (14, 22)])
        h = hours_breakdown(c, [Downtime(datetime(2026, 10, 21, 6), datetime(2026, 10, 21, 10), "maintenance"),
                                Downtime(datetime(2026, 10, 22, 6), datetime(2026, 10, 22, 8), "breakdown")],
                            datetime(2026, 10, 19), datetime(2026, 10, 26))
        self.assertEqual((h["gross"], h["planned_downtime"], h["unplanned_downtime"], h["net"]), (96.0, 4.0, 2.0, 90.0))


class Requirements(unittest.TestCase):
    def test_yield_fg_and_wip_netting(self):
        inp = clean(order("A", qty=10000))
        from mpi_aps.model import Stock
        inp.stocks = [Stock("SKU-NBR13", "PK-1", "FG", D(2000), D(500)), Stock("SKU-NBR13", "PK-1", "DIP_WIP", D(1000), D(0), D(200))]
        r = requirements(inp, Ctx(inp))[0]
        self.assertEqual(r.fg_used, 1500)                                      # 2000 - 500 allocated
        run = dict((p, (q, w)) for p, q, w in r.stages)
        self.assertEqual(run["packing"][0], int((D(8500) / D("0.995")).to_integral_value("ROUND_CEILING")))
        self.assertEqual(run["dipping"][1], 800)                              # 1000 - 200 quality-blocked
        self.assertEqual(run["dipping"][0], int((D(run["packing"][0] - 800) / D("0.97")).to_integral_value("ROUND_CEILING")))

    def test_priority_override_requires_rank_and_wins(self):
        inp = clean(order("A", prio=3, due=40), order("B", prio=1, due=10))
        from mpi_aps.planner import prioritise
        self.assertEqual([o.so for o, _, _ in prioritise(inp)], ["B", "A"])
        inp.priority_overrides["A-10"] = (1, "customer escalation", "u")
        self.assertEqual([o.so for o, _, _ in prioritise(inp)], ["A", "B"])


class SplitAndSequencing(unittest.TestCase):
    def test_lots_split_across_machines_preserve_quantity(self):
        inp = clean(order("A", qty=20000))
        inp.config.lots_by_order["A-10"] = 2
        s = generate(inp)
        k = [o for o in s.ops.values() if o.process == "knitting"]
        self.assertEqual(len(k), 2)
        self.assertEqual(len({o.resource for o in k}), 2)
        self.assertEqual(validate_schedule(inp, s), [])

    def test_changeover_applied_between_different_skus_on_same_resource(self):
        inp = clean(order("A", sku="SKU-NBR13", qty=6000), order("B", sku="SKU-LAT13", qty=6000))
        for r in inp.resources.values():
            if r.process == "knitting" and r.id != "PK-K1":
                r.active = False
        s = generate(inp)
        k = sorted((o for o in s.ops.values() if o.process == "knitting"), key=lambda o: o.start)
        self.assertEqual(k[0].setup_hours, 0)
        self.assertGreater(k[1].setup_hours, 0)

    def test_full_sample_has_no_hard_violations(self):
        inp = build_sample(PD, now=NOW)
        self.assertEqual(validate_schedule(inp, generate(inp)), [])


class DataQuality(unittest.TestCase):
    def test_clean_data_has_no_findings(self):
        self.assertEqual(val.validate(build_sample(PD, seed_defects=False, now=NOW)), [])

    def test_seeded_defects_detected_with_records(self):
        codes = {f["code"]: f for f in val.validate(build_sample(PD, now=NOW))}
        for c in ("SKU_UNKNOWN", "NO_ELIGIBLE_RESOURCE", "CURE_TIME_MISSING", "BOM_STOCK_UNIT_MISMATCH", "MISSING_EFFICIENCY", "RATE_UNVALIDATED"):
            self.assertIn(c, codes)
            self.assertTrue(codes[c]["records"] and codes[c]["action"])
        self.assertIn("BD-D2", " ".join(codes["MISSING_EFFICIENCY"]["records"]))

    def test_duplicates_negatives_and_broken_sequence(self):
        inp = clean(order("A"), order("A"))
        inp.orders.append(order("N", qty=-5))
        inp.rates.append(inp.rates[0])
        inp.routings[1] = type(inp.routings[1])(**{**inp.routings[1].__dict__, "predecessor": "packing"})
        codes = {f["code"] for f in val.validate(inp)}
        self.assertTrue({"DUPLICATE_KEY", "RATE_DUPLICATE", "NEGATIVE_QUANTITY", "BROKEN_SEQUENCE"} <= codes)

    def test_gauge_bed_mismatch_flagged(self):
        inp = clean(order("A"))
        import dataclasses
        r = next(x for x in inp.rates if x.sku == "SKU-NBR13" and x.process == "knitting")
        inp.rates.append(dataclasses.replace(r, resource="PK-K3", effective_from=r.effective_from))   # G15 machine
        self.assertIn("GAUGE_BED_MISMATCH", {f["code"] for f in val.validate(inp)})

    def test_gross_rate_uses_efficiency_hierarchy_in_engine(self):
        inp = clean(order("A", sku="SKU-NBR15", qty=12000))          # K3 gross rate, resource-level factor 0.80 (sample seed)
        inp.resources["PK-K4"].active = False                      # force the gross-rate machine
        s = generate(inp)
        op = next(o for o in s.ops.values() if o.process == "knitting" and o.resource == "PK-K3")
        rate = next(r for r in inp.rates if r.resource == "PK-K3" and r.sku == "SKU-NBR15" and r.kind.value == "demonstrated" and r.process == "knitting")
        self.assertEqual(inp.settings.resolve_efficiency("PK-1", "knitting", "PK-K3", PD).value.scope, Scope.RESOURCE)
        self.assertAlmostEqual(op.run_hours, float((D(op.run_qty) / 12 / (rate.value * D("0.80")))), places=3)
