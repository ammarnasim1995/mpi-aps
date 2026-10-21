import dataclasses
import unittest
from datetime import date

from mpi_aps import master_import as mi
from mpi_aps.auth import Principal
from mpi_aps.sample import build_sample
from mpi_aps.service import AppService, BadRequest, Forbidden
from mpi_aps.store import Store

from .fixtures import NOW, PD

ADMIN = Principal("admin", "central_planning_admin", ("*",))


def same(a, b):
    """Dataclass equality ignoring `origin` (uploads are tagged 'upload')."""
    def norm(x):
        return dataclasses.replace(x, origin="x") if hasattr(x, "origin") else x
    return norm(a) == norm(b)


class RoundTrip(unittest.TestCase):
    def test_every_entity_exports_and_reimports_identically(self):
        inp = build_sample(PD, now=NOW)
        for ent in mi.SPECS:
            text = mi.to_csv(ent, inp)
            rows, objs, rej = mi.parse_csv(ent, text)
            self.assertEqual(rej, [], f"{ent}: {rej[:2]}")
            self.assertEqual(len(objs), len(mi.to_rows(ent, inp)), ent)
            if ent in ("plants", "skus", "rates", "versions", "stocks", "raw_stocks", "receipts", "boms", "routings"):
                orig = {"plants": list(inp.plants.values()), "skus": list(inp.skus.values()), "rates": inp.rates, "versions": inp.versions,
                        "stocks": inp.stocks, "raw_stocks": inp.raw_stocks, "receipts": inp.receipts, "boms": inp.boms, "routings": inp.routings}[ent]
                for a, (b, _) in zip(orig, objs):
                    self.assertTrue(same(a, b), (ent, a, b))


class Validation(unittest.TestCase):
    def test_rejections_are_specific(self):
        rates = mi.template("rates") + (
            "S1,P1,knitting,R1,REC,demonstrated,12,cartons/minute,net,src,validated,2026-01-01,,N\n"      # bad unit
            "S1,P1,knitting,R1,REC,demonstrated,-1,dozens/machine-hour,net,src,validated,2026-01-01,,N\n"   # negative (different key below)
            "S2,P1,sewing,R1,REC,demonstrated,12,dozens/machine-hour,net,src,validated,2026-01-01,,N\n"     # bad process
            "S3,P1,knitting,R1,REC,demonstrated,12,dozens/machine-hour,net,,validated,2026-01-01,,N\n"      # missing source
            "S4,P1,knitting,R1,REC,demonstrated,12,dozens/machine-hour,net,src,validated,2026-01-01,,N\n"
            "S4,P1,knitting,R1,REC,demonstrated,13,dozens/machine-hour,net,src,validated,2026-01-01,,N\n")  # duplicate key
        rows, objs, rej = mi.parse_csv("rates", rates)
        self.assertEqual(len(objs), 1)
        msgs = " | ".join(e for r in rej for e in r["errors"])
        for frag in ("not supported", "must be > 0", "must be one of", "source is required", "duplicate key"):
            self.assertIn(frag, msgs)

    def test_missing_column_and_unknown_entity(self):
        with self.assertRaises(ValueError):
            mi.parse_csv("skus", "sku,material\nA,B\n")
        with self.assertRaises(ValueError):
            mi.parse_csv("nonsense", "a\n1\n")

    def test_plant_calendar_validation(self):
        base = "plant,country,name,shifts,working_weekdays,holidays\n"
        _, objs, rej = mi.parse_csv("plants", base + "P1,PK,Plant,6-14;14-22,\"0,1,2,3,4,5\",2026-12-25\n")
        self.assertEqual(objs[0][0].calendar.shifts, [(6.0, 14.0), (14.0, 22.0)])
        _, _, rej = mi.parse_csv("plants", base + "P2,PK,Plant,6-14;13-22,\"0,1\",\n" + "P3,PK,Plant,6-14,\"7\",\n")
        self.assertEqual(len(rej), 2)


class ServiceFlow(unittest.TestCase):
    def setUp(self):
        self.svc = AppService(Store(":memory:"), PD, now_fn=lambda: NOW)
        self.addCleanup(self.svc.store.close)

    def test_strict_import_is_all_or_nothing_and_replace_persists(self):
        before = len(self.svc.inp.boms)
        bad = mi.template("boms") + "SKU-NBR13,YARN-13,0.05,KG\nSKU-NBR15,YARN-15,abc,KG\n"
        r = self.svc.import_master(ADMIN, "boms", bad, "boms.csv")
        self.assertFalse(r["applied"])
        self.assertEqual(r["rejected"], 1)
        self.assertEqual(len(self.svc.inp.boms), before)                 # nothing applied
        good = mi.template("boms") + "SKU-NBR13,YARN-13,0.05,KG\nSKU-NBR15,YARN-15,0.04,KG\n"
        r = self.svc.import_master(ADMIN, "boms", good, "boms.csv", control_rows=2)
        self.assertTrue(r["applied"] and r["reconciled"])
        self.assertEqual(len(self.svc.inp.boms), 2)
        self.assertEqual({b.origin for b in self.svc.inp.boms}, {"upload"})
        self.svc.rebuild()                                               # persisted: survives a rebuild
        self.assertEqual(len(self.svc.inp.boms), 2)
        self.assertIn("upload", self.svc.meta(ADMIN)["data_banner"])
        self.assertTrue(r["post_import_validation"]["errors"] > 0)       # other SKUs now lack BOMs / mapping issues are surfaced, not hidden
        self.assertIn("import.master.boms", [a["action"] for a in self.svc.store.audit_list()])

    def test_non_strict_loads_valid_rows_and_reports_rejects(self):
        csv = mi.template("receipts") + "YARN-13,PK-1,2026-11-01,5000\nYARN-13,PK-1,2026-11-02,-5\n"
        r = self.svc.import_master(ADMIN, "receipts", csv, "r.csv", strict=False)
        self.assertTrue(r["applied"])
        self.assertEqual((r["accepted"], r["rejected"]), (1, 1))
        self.assertFalse(r["reconciled"] and r["rejected"] == 0)

    def test_revert_to_sample_and_permissions(self):
        self.svc.import_master(ADMIN, "material_rules", mi.template("material_rules") + "YARN-13,block\n", "m.csv")
        self.assertEqual(list(self.svc.inp.material_rules), ["YARN-13"])
        self.svc.revert_master(ADMIN, "material_rules", "back to sample")
        self.assertIn("YARN-15", self.svc.inp.material_rules)
        with self.assertRaises(Forbidden):
            self.svc.import_master(Principal("p", "plant_planner", ("PK-1",)), "boms", mi.template("boms"), "x.csv")
        with self.assertRaises(BadRequest):
            self.svc.import_master(ADMIN, "nonsense", "a\n1\n", "x.csv")

    def test_origin_follows_upload_channel_not_source_text(self):
        self.assertIn("SAMPLE DATA", self.svc.meta(ADMIN)["data_banner"])
        self.svc.import_master(ADMIN, "rates", mi.to_csv("rates", self.svc.inp), "rates.csv")     # content still says SAMPLE-SEED
        self.assertIn("upload", self.svc.meta(ADMIN)["data_banner"])
        self.assertTrue(self.svc.meta(ADMIN)["data_banner"].startswith("Mixed"))

    def test_status_reports_sample_vs_upload(self):
        st = {s["entity"]: s for s in self.svc.master_status(ADMIN)}
        self.assertEqual(st["rates"]["source"], "sample")
        self.svc.import_master(ADMIN, "skus", mi.to_csv("skus", self.svc.inp), "skus.csv")
        st = {s["entity"]: s for s in self.svc.master_status(ADMIN)}
        self.assertEqual((st["skus"]["source"], st["skus"]["file"]), ("upload", "skus.csv"))


if __name__ == "__main__":
    unittest.main()
