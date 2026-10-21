"""Synthetic values only (SYNTHETIC-TEST). Not Midas rates, factors or cure times."""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from mpi_aps_core.curing import QualityStatus, earliest_packing_start
from mpi_aps_core.freshness import Origin, RefreshRecord, State, acknowledge_stale, assess
from mpi_aps_core.rates import (ProductionRate, RateBasis, RateKind, RateValidation, required_hours,
                                select_rate)
from mpi_aps_core.settings import Approval, CureRule, EfficiencyFactor, FreshnessPolicy, Scope, SettingsStore
from mpi_aps_core.units import UomConverter, parse_rate_unit

D0 = date(2026, 1, 1)
NOW = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def store():
    return SettingsStore(clock=lambda: NOW)


def rate(**kw):
    base = dict(sku="S1", plant="P1", process="knitting", resource="K1", recipe="R1",
                kind=RateKind.DEMONSTRATED, value=D(20), unit="dozens/machine-hour", basis=RateBasis.NET,
                source="SYNTHETIC-TEST", validation_status=RateValidation.VALIDATED, effective_from=D0)
    base.update(kw)
    return ProductionRate(**base)


def eff(scope, f, **kw):
    return EfficiencyFactor(scope=scope, factor=D(f), effective_from=D0, source="SYNTHETIC-TEST",
                            approval_status=kw.pop("approval_status", Approval.APPROVED), **kw)


class Units(unittest.TestCase):
    def test_dozen_pair(self):
        v, _ = UomConverter().convert("M1", D(1200), "PR", "DZ")
        self.assertEqual(v, D(100))
        v, _ = UomConverter().convert("M1", D(5), "dozens", "pairs")
        self.assertEqual(v, D(60))

    def test_material_specific_and_missing(self):
        c = UomConverter({"M1": {"CAR": D(144)}})
        self.assertEqual(c.convert("M1", D(2), "CAR", "PR")[0], D(288))
        v, iss = c.convert("M2", D(2), "CAR", "PR")
        self.assertIsNone(v)
        self.assertEqual(iss[0].code, "UOM_CONVERSION_MISSING")

    def test_nonstandard_dozen_warns_and_is_used(self):
        v, iss = UomConverter({"M1": {"DZ": D(10)}}).convert("M1", D(1), "DZ", "PR")
        self.assertEqual(v, D(10))
        self.assertIn("NONSTANDARD_DOZEN", [i.code for i in iss])

    def test_negative_and_bad_rate_unit(self):
        self.assertIsNone(UomConverter().convert("M1", D(-1), "PR", "DZ")[0])
        self.assertIsNone(parse_rate_unit("cartons/minute")[0])
        self.assertEqual(parse_rate_unit("pairs/hour")[0].qty_unit, "PR")


class Hours(unittest.TestCase):
    def calc(self, r, s=None, **kw):
        return required_hours(qty=kw.pop("qty", D(1200)), qty_unit=kw.pop("qty_unit", "PR"), material="M1",
                              rate=r, settings=s or store(), converter=UomConverter(), as_of=D0, **kw)

    def test_net_rate_pairs_converted_to_dozens(self):
        res = self.calc(rate())  # 1200 PR = 100 DZ / 20 DZ/h
        self.assertTrue(res.ok)
        self.assertEqual(res.total_hours, D("5.0000"))
        self.assertIsNone(res.efficiency_factor)  # no deduction applied to a net rate

    def test_resource_specific_rates_differ(self):  # acceptance #3
        a = self.calc(rate(resource="K1", value=D(20)))
        b = self.calc(rate(resource="K2", value=D(25)))
        self.assertEqual((a.total_hours, b.total_hours), (D("5.0000"), D("4.0000")))

    def test_gross_rate_applies_factor_once(self):
        s = store()
        s.put_efficiency_factor("admin", eff(Scope.PLANT, "0.8", plant="P1"), "init")
        res = self.calc(rate(basis=RateBasis.GROSS), s)
        self.assertEqual(res.total_hours, D("6.2500"))
        self.assertEqual(res.efficiency_scope, "plant")

    def test_gross_without_factor_blocked_not_defaulted(self):
        res = self.calc(rate(basis=RateBasis.GROSS))
        self.assertFalse(res.ok)
        self.assertIsNone(res.total_hours)
        self.assertIn("MISSING_EFFICIENCY", [i.code for i in res.issues])

    def test_unknown_basis_and_unvalidated_blocked(self):
        self.assertIn("RATE_BASIS_UNKNOWN", [i.code for i in self.calc(rate(basis=RateBasis.UNKNOWN)).issues])
        r = rate(validation_status=RateValidation.UNVALIDATED)
        self.assertFalse(self.calc(r).ok)
        ov = self.calc(r, allow_unvalidated=True)
        self.assertTrue(ov.ok)
        self.assertIn("RATE_UNVALIDATED_USED", [i.code for i in ov.issues])

    def test_setup_separate_and_not_double_counted(self):
        self.assertEqual(self.calc(rate(), setup_hours=D("1.5")).total_hours, D("6.5000"))
        inc = self.calc(rate(includes_setup=True), setup_hours=D("1.5"))
        self.assertEqual(inc.total_hours, D("5.0000"))
        self.assertEqual(inc.setup_hours, D(0))

    def test_unconvertible_unit_blocks(self):
        res = self.calc(rate(), qty=D(3), qty_unit="CAR")
        self.assertFalse(res.ok)

    def test_select_rate_missing_duplicate_effective_dating(self):
        kw = dict(sku="S1", plant="P1", process="knitting", resource="K1", recipe="R1",
                  kind=RateKind.DEMONSTRATED, as_of=D0)
        self.assertEqual(select_rate([], **kw)[1][0].code, "RATE_MISSING")
        self.assertEqual(select_rate([rate(), rate()], **kw)[1][0].code, "RATE_DUPLICATE")
        self.assertEqual(select_rate([rate(resource="K2")], **kw)[1][0].code, "RATE_MISSING")  # no substitution
        self.assertEqual(select_rate([rate(effective_from=date(2027, 1, 1))], **kw)[1][0].code, "RATE_MISSING")
        self.assertEqual(select_rate([rate(kind=RateKind.SAP_STANDARD)], **kw)[1][0].code, "RATE_MISSING")


class Efficiency(unittest.TestCase):
    def test_hierarchy_order(self):
        s = store()
        s.put_efficiency_factor("a", eff(Scope.SYSTEM_DEFAULT, "0.5"), "r")
        s.put_efficiency_factor("a", eff(Scope.PLANT, "0.7", plant="P1"), "r")
        self.assertEqual(s.resolve_efficiency("P1", "knitting", "K1", D0).value.factor, D("0.7"))
        s.put_efficiency_factor("a", eff(Scope.PLANT_PROCESS, "0.8", plant="P1", process="knitting"), "r")
        self.assertEqual(s.resolve_efficiency("P1", "knitting", "K1", D0).value.factor, D("0.8"))
        self.assertEqual(s.resolve_efficiency("P1", "dipping", "L1", D0).value.factor, D("0.7"))
        s.put_efficiency_factor("a", eff(Scope.RESOURCE, "0.9", plant="P1", resource="K1"), "r")
        self.assertEqual(s.resolve_efficiency("P1", "knitting", "K1", D0).value.factor, D("0.9"))
        self.assertEqual(s.resolve_efficiency("P1", "knitting", "K2", D0).value.factor, D("0.8"))
        self.assertEqual(s.resolve_efficiency("P2", "knitting", "K1", D0).value.factor, D("0.5"))

    def test_draft_ignored_and_default_requires_approval(self):
        s = store()
        s.put_efficiency_factor("a", eff(Scope.SYSTEM_DEFAULT, "0.5", approval_status=Approval.DRAFT), "r")
        s.put_efficiency_factor("a", eff(Scope.RESOURCE, "0.9", plant="P1", resource="K1",
                                         approval_status=Approval.DRAFT), "r")
        res = s.resolve_efficiency("P1", "knitting", "K1", D0)
        self.assertIsNone(res.value)
        self.assertIn("MISSING_EFFICIENCY", [i.code for i in res.issues])

    def test_conflict_not_silently_resolved(self):
        s = store()
        s.put_efficiency_factor("a", eff(Scope.PLANT, "0.7", plant="P1"), "r")
        f2 = EfficiencyFactor(Scope.PLANT, D("0.6"), date(2025, 1, 1), "SYNTHETIC-TEST", Approval.APPROVED, plant="P1")
        s.put_efficiency_factor("a", f2, "r")
        res = s.resolve_efficiency("P1", "knitting", "K1", D0)
        self.assertEqual(res.issues[-1].code, "EFFICIENCY_CONFLICT")

    def test_fallback_requires_authorised_role_and_is_audited(self):
        s = store()
        f = eff(Scope.RESOURCE, "0.75", plant="P1", resource="K9", approval_status=Approval.DRAFT)
        with self.assertRaises(PermissionError):
            s.approve_fallback_efficiency("u", "management_viewer", f, "x")
        s.approve_fallback_efficiency("planner1", "central_planner", f, "K9 new machine, interim")
        self.assertEqual(s.resolve_efficiency("P1", "knitting", "K9", D0).value.factor, D("0.75"))
        self.assertEqual(s.audit[-1].reason, "K9 new machine, interim")

    def test_validation_audit_and_recalculation_scopes(self):
        s = store()
        with self.assertRaises(ValueError):
            s.put_efficiency_factor("a", eff(Scope.PLANT, "1.2", plant="P1"), "r")
        with self.assertRaises(ValueError):
            s.put_efficiency_factor("a", eff(Scope.PLANT, "0.8", plant="P1"), "  ")
        with self.assertRaises(ValueError):
            s.put_efficiency_factor("a", eff(Scope.RESOURCE, "0.8", plant="P1"), "r")  # resource missing
        v0 = s.version
        s.put_efficiency_factor("a", eff(Scope.PLANT, "0.8", plant="P1"), "r")
        s.put_efficiency_factor("a", eff(Scope.PLANT, "0.85", plant="P1"), "tune")
        self.assertEqual(s.audit[-1].previous.factor, D("0.8"))
        self.assertEqual(s.affected_scopes_since(v0), ["efficiency:plant:P1:None:None"])


class Curing(unittest.TestCase):
    def setUp(self):
        self.s = store()
        self.done = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)

    def go(self, **kw):
        args = dict(dipping_completion=self.done, plant="P1", sku="S1", recipe="R1", coating="C1",
                    quality=QualityStatus.RELEASED, quality_released_at=None, settings=self.s, as_of=D0)
        args.update(kw)
        return earliest_packing_start(**args)

    def rule(self, hours, **kw):
        self.s.put_cure_rule("a", CureRule(min_cure_hours=D(hours), effective_from=D0, source="SYNTHETIC-TEST",
                                           approval_status=Approval.APPROVED, **kw), "init")

    def test_missing_rule_blocks(self):
        r = self.go()
        self.assertIsNone(r.earliest_start)
        self.assertEqual(r.issues[0].code, "CURE_TIME_MISSING")

    def test_start_is_completion_plus_cure_plus_additional(self):
        self.rule(6, sku="S1", additional_processing_hours=D(2))
        self.assertEqual(self.go().earliest_start, self.done + timedelta(hours=8))

    def test_most_specific_rule_wins(self):
        self.rule(24)  # generic
        self.rule(4, sku="S1", coating="C1")
        self.assertEqual(self.go().earliest_start, self.done + timedelta(hours=4))
        self.assertEqual(self.go(sku="S2").earliest_start, self.done + timedelta(hours=24))

    def test_conflict_and_quality_gates(self):
        self.rule(4, sku="S1")
        self.rule(5, coating="C1")  # equal specificity (1 field each)
        self.assertEqual(self.go().issues[0].code, "CURE_RULE_CONFLICT")
        s2 = store(); self.s = s2; self.rule(4, sku="S1")
        for q in (QualityStatus.PENDING, QualityStatus.BLOCKED, QualityStatus.UNKNOWN):
            self.assertEqual(self.go(quality=q).issues[0].code, "QUALITY_NOT_RELEASED")

    def test_late_quality_release_governs(self):
        self.rule(4, sku="S1")
        late = self.done + timedelta(hours=10)
        self.assertEqual(self.go(quality_released_at=late).earliest_start, late)


class Freshness(unittest.TestCase):
    def rec(self, hrs_ago, ok=True, origin=Origin.LIVE_SAP, src="orders"):
        return RefreshRecord(src, origin, NOW - timedelta(hours=hrs_ago), ok, "f.xlsx" if origin == Origin.UPLOAD else None,
                             () if ok else ("timeout",))

    def test_fresh_stale_boundary(self):
        s = store()
        self.assertEqual(assess("orders", [self.rec(24)], NOW, s).state, State.FRESH)
        a = assess("orders", [self.rec(25)], NOW, s)
        self.assertEqual(a.state, State.STALE)
        self.assertNotIn("LIVE -", a.label)

    def test_failed_refresh_keeps_last_success(self):
        a = assess("orders", [self.rec(10), self.rec(1, ok=False)], NOW, store())
        self.assertTrue(a.last_attempt_failed)
        self.assertEqual(a.last_success_at, NOW - timedelta(hours=10))
        self.assertEqual(a.last_attempt_at, NOW - timedelta(hours=1))
        self.assertEqual(a.errors, ("timeout",))

    def test_sample_never_live_and_never_refreshed(self):
        self.assertTrue(assess("orders", [self.rec(1, origin=Origin.SAMPLE)], NOW, store()).label.startswith("SAMPLE"))
        self.assertEqual(assess("orders", [], NOW, store()).state, State.NEVER_REFRESHED)

    def test_upload_label_has_file_and_threshold_overrides(self):
        s = store()
        self.assertIn("f.xlsx", assess("orders", [self.rec(1, origin=Origin.UPLOAD)], NOW, s).label)
        s.put_freshness_policy("a", FreshnessPolicy("orders", D(48)), "weekly files")
        self.assertEqual(assess("orders", [self.rec(30)], NOW, s).state, State.FRESH)
        s.put_freshness_policy("a", FreshnessPolicy("orders", D(2), process="dipping"), "tight")
        self.assertEqual(assess("orders", [self.rec(30)], NOW, s, process="dipping").state, State.STALE)

    def test_critical_warning_and_acknowledgment_audited(self):
        s = store()
        s.put_freshness_policy("a", FreshnessPolicy("inventory", critical=True, acknowledgment_permitted=True,
                                                    affected_decisions=("material availability",)), "init")
        a = assess("inventory", [self.rec(30, src="inventory")], NOW, s)
        self.assertTrue(a.critical_warning)
        self.assertEqual(a.affected_decisions, ("material availability",))
        n = len(s.audit)
        acknowledge_stale(s, a, "planner1", "month-end, accepted")
        self.assertEqual(len(s.audit), n + 1)
        self.assertEqual(s.audit[-1].action, "acknowledge_stale_data")
        self.assertEqual(s.affected_scopes_since(0).count("event"), 0)

    def test_ack_forbidden_by_policy(self):
        s = store()
        s.put_freshness_policy("a", FreshnessPolicy("orders", critical=True), "init")
        a = assess("orders", [self.rec(30)], NOW, s)
        with self.assertRaises(PermissionError):
            acknowledge_stale(s, a, "u", "r")


if __name__ == "__main__":
    unittest.main()
