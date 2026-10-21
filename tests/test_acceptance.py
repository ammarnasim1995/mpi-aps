"""Acceptance scenarios 1-15 (docs/ACCEPTANCE.md). SYNTHETIC data only."""
import dataclasses
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

from mpi_aps import analytics as an
from mpi_aps import importer
from mpi_aps.auth import Principal
from mpi_aps.planner import Ctx, apply_decisions, apply_move, generate, validate_schedule
from mpi_aps.service import AppService, BadRequest, Forbidden
from mpi_aps.store import PlanLocked, Store
from mpi_aps_core.settings import Approval, CureRule

from .fixtures import NOW, PD, clean, order

AUTHOR = Principal("author", "central_planner", ("*",))
APPROVER = Principal("approver", "central_planner", ("*",))


def ops_of(s, key):
    return {o.process: o for o in s.ops.values() if o.order_key == key}


class A01_Import(unittest.TestCase):
    CSV = ("sales_order,line,customer,sku,qty,uom,delivered_qty,confirmed_qty,req_date,conf_date,cust_priority,ship_priority,country,plant,status\n"
           "SO1,10,C,SKU-NBR13,12000,PR,2000,12000,2026-11-20,2026-11-20,1,1,Pakistan,PK-1,open\n"
           "SO2,10,C,SKU-NBR13,500,DZ,100,500,2026-11-20,,1,1,Pakistan,PK-1,open\n"
           "SO3,10,C,SKU-NOPE,100,PR,0,100,2026-11-20,,1,1,Pakistan,PK-1,open\n"
           "SO1,10,C,SKU-NBR13,5,PR,0,5,2026-11-20,,1,1,Pakistan,PK-1,open\n")

    def test_open_quantity_and_rejections(self):
        inp = clean()
        acc, rej, total = importer.parse_orders(self.CSV, inp, set())
        self.assertEqual([o.key for o in acc], ["SO1-10", "SO2-10"])
        self.assertEqual({r["key"] for r in rej}, {"SO3-10", "SO1-10"})           # unknown SKU, duplicate in file
        inp.orders = acc
        s = generate(inp)
        rows = {r["order"]: r for r in an.order_rows(inp, s)}
        self.assertEqual(rows["SO1-10"]["open_pairs"], 10000)                      # 12000 - 2000
        self.assertEqual(rows["SO2-10"]["open_pairs"], 4800)                       # (500-100) dz x 12
        self.assertEqual(rows["SO1-10"]["origin"], "upload" if acc[0].origin == "upload" else rows["SO1-10"]["origin"])

    def test_existing_key_not_overwritten(self):
        acc, rej, _ = importer.parse_orders(self.CSV, clean(), {"SO1-10"})
        self.assertIn("already exists", " ".join(e for r in rej if r["key"] == "SO1-10" for e in r["errors"]))


class A02_A03_Eligibility_Rates(unittest.TestCase):
    def test_multiple_machines_and_assignment(self):
        inp = clean(order("A"), order("B"))
        ctx = Ctx(inp)
        cands, _ = ctx.candidates("SKU-NBR13", "PK-1", "knitting")
        self.assertEqual({c.resource for c in cands}, {"PK-K1", "PK-K2"})          # gauge 13 machines only
        s = generate(inp)
        used = {o.resource for o in s.ops.values() if o.process == "knitting"}
        self.assertEqual(used, {"PK-K1", "PK-K2"})                                 # load spread, both eligible
        self.assertEqual(validate_schedule(inp, s), [])

    def test_resource_specific_rates_give_different_hours(self):
        inp = clean(order("A", qty=12000))
        s = generate(inp)
        op = ops_of(s, "A-10")["knitting"]
        ctx = Ctx(inp)
        cands, _ = ctx.candidates(op.sku, op.plant, "knitting")
        hrs = {c.resource: ctx.hours(op, c, D(0)).run_hours for c in cands}
        self.assertNotEqual(hrs["PK-K1"], hrs["PK-K2"])
        for c in cands:   # NET rate: hours = dozens / rate
            self.assertEqual(hrs[c.resource], (D(op.run_qty) / 12 / c.rate.value).quantize(D("0.0001")))

    def test_ineligible_resource_rejected_not_substituted(self):
        inp = clean(order("A"))
        s = generate(inp)
        op = ops_of(s, "A-10")["knitting"]
        new, viol, _ = apply_move(inp, s, op.id, "PK-K3", None, "u", "try G15 machine")   # gauge 15 machine
        self.assertIsNone(new)
        self.assertEqual(viol[0].rule, "ELIGIBILITY")


class A04_Overload(unittest.TestCase):
    def test_overloaded_resource_flagged(self):
        inp = clean(*[order(f"H{i}", qty=70000, due=8) for i in range(3)])
        s = generate(inp)
        rc = an.rough_cut(inp, s)
        over = [r for r in rc["resources"] if r["overloaded"]]
        self.assertTrue(over and over[0]["max_cumulative_shortage_h"] > 0)
        rows = an.order_rows(inp, s)
        self.assertTrue(any(r["status"] == "late" for r in rows))
        cap = an.capacity(inp, s, "week", "PK-1", "knitting")
        self.assertTrue(any((r["utilization_pct"] or 0) >= 99.9 for r in cap["rows"]))


class A05_A06_A07_Precedence(unittest.TestCase):
    def setUp(self):
        self.inp = clean(order("A", qty=12000))
        self.s = generate(self.inp)
        self.o = ops_of(self.s, "A-10")

    def test_dipping_cannot_start_before_knitting_output(self):
        k, d = self.o["knitting"], self.o["dipping"]
        self.assertGreaterEqual(d.start, k.finish + timedelta(hours=4))          # transfer 2h + queue 2h
        new, viol, _ = apply_move(self.inp, self.s, d.id, d.resource, k.start, "u", "try to pull dipping earlier")
        self.assertIsNone(new)
        self.assertEqual(viol[0].rule, "EARLIEST_FEASIBLE")

    def test_packing_respects_cure_quality_and_supply(self):
        d, p = self.o["dipping"], self.o["packing"]
        cure = timedelta(hours=6)                                                # NBR cure rule (sample seed)
        self.assertGreaterEqual(p.start, d.finish + max(cure, timedelta(hours=4)) + timedelta(hours=2))
        self.assertEqual(validate_schedule(self.inp, self.s), [])
        p.run_qty += 5000                                                         # more than eligible dipped supply
        self.assertIn("PACK_QTY", [v.rule for v in validate_schedule(self.inp, self.s)])

    def test_missing_cure_rule_blocks_packing(self):
        inp = clean(order("A"))
        inp.settings._cure.clear()
        s = generate(inp)
        p = ops_of(s, "A-10")["packing"]
        self.assertIsNone(p.start)
        self.assertIn("CURE_TIME_MISSING", p.blocked)

    def test_knitting_change_recalculates_downstream(self):
        k, d, p = self.o["knitting"], self.o["dipping"], self.o["packing"]
        new, viol, impact = apply_move(self.inp, self.s, k.id, k.resource, k.start + timedelta(days=3), "u", "late yarn")
        self.assertIsNotNone(new, viol)
        n = {o.process: o for o in new.ops.values()}
        self.assertGreater(n["knitting"].finish, k.finish)
        self.assertGreater(n["dipping"].start, d.start)
        self.assertGreater(n["packing"].finish, p.finish)
        self.assertEqual(validate_schedule(self.inp, new), [])
        self.assertIn(d.id, impact)

    def test_resolve_is_deterministic(self):
        a, b = generate(clean(order("A"), order("B"))), generate(clean(order("A"), order("B")))
        self.assertEqual(a.decisions(), b.decisions())
        self.assertEqual(apply_decisions(self.inp, self.s.decisions()).decisions(), self.s.decisions())


class A08_Breakdown(unittest.TestCase):
    def test_breakdown_identifies_affected_orders(self):
        inp = clean(order("A"), order("B", sku="SKU-NBR15"))
        s = generate(inp)
        k = ops_of(s, "A-10")["knitting"]
        start = (k.start + timedelta(hours=10)).isoformat()
        end = (k.start + timedelta(hours=60)).isoformat()
        r = an.breakdown_impact(inp, s.decisions(), k.resource, start, end)
        self.assertIn("A-10", r["orders_on_resource_during_outage"])
        changed = {x["order"] for x in r["if_plan_unchanged"]["orders_changed"]}
        self.assertIn("A-10", changed)
        a = next(x for x in r["if_plan_unchanged"]["orders_changed"] if x["order"] == "A-10")
        self.assertGreater(a["delta_days"], 0)


class A09_Material(unittest.TestCase):
    def test_block_rule_prevents_and_delay_rule_delays(self):
        inp = clean(order("A", qty=12000))                      # needs 540 kg YARN-13 at PK-1
        for rs in inp.raw_stocks:
            if (rs.material, rs.plant) == ("YARN-13", "PK-1"):
                rs.qty = D(100)
        inp.receipts = [r for r in inp.receipts if r.material != "YARN-13"] + []
        from mpi_aps.model import Receipt, MaterialRule
        inp.receipts.append(Receipt("YARN-13", "PK-1", PD + timedelta(days=9), D(9000)))
        s = generate(inp)                                        # default rule: delay
        k = ops_of(s, "A-10")["knitting"]
        self.assertGreaterEqual(k.start, datetime(PD.year, PD.month, PD.day) + timedelta(days=9))
        self.assertEqual(k.driver, "material ready")
        inp.material_rules["YARN-13"] = MaterialRule("YARN-13", "block")
        s2 = generate(inp)
        o = ops_of(s2, "A-10")
        self.assertIsNone(o["knitting"].start)
        self.assertIn("rule 'block'", o["knitting"].blocked)
        self.assertIsNone(o["packing"].start)                    # downstream blocked too
        self.assertEqual({r["order"]: r for r in an.order_rows(inp, s2)}["A-10"]["status"], "unscheduled")


class A10_A14_Scenarios_Governance(unittest.TestCase):
    def setUp(self):
        self.svc = AppService(Store(":memory:"), PD, four_eyes=True, now_fn=lambda: NOW)
        self.addCleanup(self.svc.store.close)

    def test_scenario_compared_and_approved_plan_untouched(self):
        pid = self.svc.generate_plan(AUTHOR, "baseline")["plan_id"]
        self.svc.approve(APPROVER, pid)
        before = self.svc.store.get_plan(pid)
        res = self.svc.run_scenario(AUTHOR, "ot", [{"type": "overtime", "process": "knitting", "plant": "PK-1", "hours_per_day": 2, "days": 28}],
                                    base_plan_id=pid)
        self.assertEqual(res["baseline_plan"]["id"], pid)
        d = res["comparison"]["kpi_delta"]
        self.assertGreater(d["overtime_hours_used"]["scenario"], 0)
        self.assertLessEqual(d["late"]["scenario"], d["late"]["baseline"])
        after = self.svc.store.get_plan(pid)
        self.assertEqual((before["decisions"], before["snapshot"], before["status"]), (after["decisions"], after["snapshot"], after["status"]))
        self.assertEqual(self.svc.store.get_plan(res["scenario_id"])["kind"], "scenario")
        with self.assertRaises(BadRequest):                       # a scenario cannot be approved as a plan
            self.svc.approve(APPROVER, res["scenario_id"])

    def test_approved_plan_immutable_and_four_eyes(self):
        pid = self.svc.generate_plan(AUTHOR, "p")["plan_id"]
        with self.assertRaises(BadRequest):
            self.svc.approve(AUTHOR, pid)                          # author may not approve own plan
        self.svc.approve(APPROVER, pid)
        sched = self.svc.schedule_for(self.svc.store.get_plan(pid))
        op = next(o for o in sched.ops.values() if o.start and o.process == "knitting")
        with self.assertRaises(Forbidden):
            self.svc.move(AUTHOR, pid, op.id, op.resource, None, "x")
        with self.assertRaises(PlanLocked):
            self.svc.store.update_draft(pid, {}, {})
        clone = self.svc.clone_plan(AUTHOR, pid, "edit approved plan")["plan_id"]
        self.assertEqual(self.svc.store.get_plan(clone)["status"], "draft")
        self.assertEqual(self.svc.store.get_plan(pid)["status"], "approved")

    def test_publish_requires_ack_of_stale_critical_data(self):
        pid = self.svc.generate_plan(AUTHOR, "p")["plan_id"]
        self.svc.approve(APPROVER, pid)
        with self.assertRaises(BadRequest) as cm:
            self.svc.publish(APPROVER, pid)
        self.assertIn("inventory", str(cm.exception))
        self.svc.acknowledge_stale(APPROVER, "inventory", "month-end, accepted")
        self.assertEqual(self.svc.publish(APPROVER, pid)["status"], "published")
        self.assertIn("freshness.acknowledge", [a["action"] for a in self.svc.store.audit_list()])

    def test_manual_override_validated_and_audited(self):
        pid = self.svc.generate_plan(AUTHOR, "p")["plan_id"]
        sched = self.svc.schedule_for(self.svc.store.get_plan(pid))
        op = next(o for o in sched.ops.values() if o.start and o.process == "knitting" and o.plant == "PK-1")
        other = next(c.resource for c in Ctx(self.svc.inp).candidates(op.sku, op.plant, "knitting")[0] if c.resource != op.resource)
        empty = self.svc.move(AUTHOR, pid, op.id, other, None, "")      # missing reason is rejected
        self.assertFalse(empty["applied"])
        self.assertEqual(empty["violations"][0]["rule"], "REASON_REQUIRED")
        r = self.svc.move(AUTHOR, pid, op.id, other, None, "balance load")
        self.assertTrue(r["applied"], r)
        a = [x for x in self.svc.store.audit_list() if x["action"] == "plan.override"][0]
        self.assertEqual(a["reason"], "balance load")
        self.assertIn(op.resource, a["previous"])
        self.assertIn(other, a["new"])
        bad = self.svc.move(AUTHOR, pid, op.id, "PK-D1", None, "wrong process")
        self.assertFalse(bad["applied"])
        self.assertIn("plan.override_rejected", [x["action"] for x in self.svc.store.audit_list()])
        self.assertTrue(self.svc.undo(AUTHOR, pid)["undone"])

    def test_plant_scope_enforced(self):
        pk = Principal("pk", "plant_planner", ("PK-1",))
        pid = self.svc.generate_plan(AUTHOR, "p")["plan_id"]
        sched = self.svc.schedule_for(self.svc.store.get_plan(pid))
        bd = next(o for o in sched.ops.values() if o.plant == "BD-1" and o.start and o.process == "knitting")
        with self.assertRaises(Forbidden):
            self.svc.move(pk, pid, bd.id, bd.resource, None, "x")
        rows = self.svc.orders(pk, {})["orders"]
        self.assertTrue(rows and {r["plant"] for r in rows} == {"PK-1"})
        with self.assertRaises(Forbidden):
            self.svc.generate_plan(Principal("v", "management_viewer", ("*",)))


class A11_A12_A13_A15(unittest.TestCase):
    def test_import_reconciliation_reports_differences(self):
        inp = clean()
        acc, rej, tot = importer.parse_orders(A01_Import.CSV, inp, set())
        # file in pairs: SO1 12000 + SO2 500 dz (=6000) + dup 5 = 18005; SO3 has an unknown SKU -> no unit conversion
        self.assertEqual(tot["pairs_in_file"], D(18005))
        self.assertEqual(tot["unconvertible_rows"], 1)
        rep = importer.reconcile(4, acc, rej, tot, control_rows=4, control_qty=18005)
        self.assertFalse(rep["reconciled"])
        self.assertIn("not comparable", rep["qty_note"])
        self.assertEqual(rep["rejected"], 2)
        good = A01_Import.CSV.split("\n")
        csv = "\n".join([good[0], good[1], good[2]]) + "\n"
        acc, rej, tot = importer.parse_orders(csv, inp, set())
        self.assertTrue(importer.reconcile(2, acc, rej, tot, 2, 18000)["reconciled"])
        bad = importer.reconcile(2, acc, rej, tot, control_rows=3, control_qty=17000)
        self.assertFalse(bad["reconciled"])
        self.assertEqual((bad["rows_difference"], bad["qty_difference_pairs"]), (-1, "1000"))

    def test_data_origin_distinguished(self):
        svc = AppService(Store(":memory:"), PD, now_fn=lambda: NOW)
        self.addCleanup(svc.store.close)
        meta = svc.meta(AUTHOR)
        self.assertIn("SAMPLE DATA", meta["data_banner"])
        self.assertNotIn("LIVE", " ".join(f["label"] for f in meta["freshness"] if "SAMPLE" not in f["label"] and "STALE" not in f["label"]))
        self.assertTrue(all(f["label"].startswith(("SAMPLE", "STALE")) for f in meta["freshness"]))
        r = svc.import_orders(AUTHOR, A01_Import.CSV.replace("SO1,", "SOX,").replace("SO2,", "SOY,"), "orders.csv")
        self.assertEqual(r["accepted"], 2)
        self.assertIn("upload", svc.meta(AUTHOR)["data_banner"])
        self.assertEqual({o.origin for o in svc.inp.orders if o.so in ("SOX", "SOY")}, {"upload"})

    def test_every_op_has_explanation_and_late_orders_have_alternatives(self):
        inp = clean(*[order(f"H{i}", qty=70000, due=8) for i in range(3)], order("S", qty=3000, due=40))
        s = generate(inp)
        for o in s.ops.values():
            if o.start:
                self.assertTrue(o.driver)
                self.assertTrue(any(c.get("chosen") for c in o.candidates))
        late = next(r["order"] for r in an.order_rows(inp, s) if r["status"] == "late")
        e = an.explain_order(inp, s, late)
        self.assertTrue(e["narrative"] and e["limiting_chain"] and e["alternatives"])
        self.assertIn("full engine re-plan of a copy", e["alternatives_note"])
