"""SYNTHETIC sample data. Every record is tagged origin='sample'. NOT Midas data and NOT SAP data.

Rates, factors, cure times, changeovers and stock are invented to exercise the workflow. A few defects
are seeded on purpose (`seed_defects=True`) so the data-quality screens have something to show.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from mpi_aps_core.freshness import Origin
from mpi_aps_core.rates import ProductionRate, RateBasis, RateKind, RateValidation
from mpi_aps_core.settings import (Approval, CureRule, EfficiencyFactor, FreshnessPolicy, Scope,
                                   SettingsStore)
from mpi_aps_core.units import UomConverter

from .model import (BomLine, CalendarSpec, ChangeoverRule, Downtime, MaterialRule, Order, Plant,
                    PlanInput, PlannerConfig, ProductionVersion, RawStock, Receipt, RefreshInfo,
                    Resource, RoutingOp, Sku, Stock)

SEED = "SAMPLE-SEED"


def build_sample(plan_date: date, seed_defects: bool = True, now: datetime | None = None) -> PlanInput:
    now = now or datetime.now(timezone.utc)
    d = lambda n: plan_date + timedelta(days=n)
    plants = {
        "PK-1": Plant("PK-1", "Pakistan", "Karachi Plant (sample)",
                      CalendarSpec([(6, 14), (14, 22)], frozenset(range(6)), frozenset({d(11)}))),
        "BD-1": Plant("BD-1", "Bangladesh", "Dhaka Plant (sample)",
                      CalendarSpec([(7, 15), (15, 23)], frozenset(range(6)), frozenset({d(16)}))),
        "LK-1": Plant("LK-1", "Sri Lanka", "Colombo Plant (sample)",
                      CalendarSpec([(6, 14), (14, 22)], frozenset(range(5)), frozenset({d(9)}))),
    }
    res: dict[str, Resource] = {}
    gauges = [("13", "S"), ("13", "S"), ("15", "S"), ("15", "S"), ("18", "S"), ("18", "S")]
    for p in plants:
        pc = p.split("-")[0]
        for i, (g, b) in enumerate(gauges, 1):
            res[f"{pc}-K{i}"] = Resource(f"{pc}-K{i}", p, "knitting", f"KNIT-G{g}", f"Knitting machine {i} (G{g})", g, b)
        for i in (1, 2):
            res[f"{pc}-D{i}"] = Resource(f"{pc}-D{i}", p, "dipping", "DIP", f"Dipping line {i}")
        for i in (1, 2):
            res[f"{pc}-P{i}"] = Resource(f"{pc}-P{i}", p, "packing", "PACK", f"Packing line {i}")
    res["PK-K2"].downtimes.append(Downtime(datetime.combine(d(3), datetime.min.time()) + timedelta(hours=6),
                                           datetime.combine(d(4), datetime.min.time()) + timedelta(hours=22),
                                           "maintenance", "Planned PM (sample)"))
    skus = {
        "SKU-NBR13": Sku("SKU-NBR13", "MAT-1001", "Nitrile coated G13 (sample)", "NITRILE", "NBR", "13", "S"),
        "SKU-NBR15": Sku("SKU-NBR15", "MAT-1002", "Nitrile coated G15 (sample)", "NITRILE", "NBR", "15", "S"),
        "SKU-PU18": Sku("SKU-PU18", "MAT-1003", "PU coated G18 (sample)", "PU", "PU", "18", "S"),
        "SKU-LAT13": Sku("SKU-LAT13", "MAT-1004", "Latex crinkle G13 (sample)", "LATEX", "LATEX", "13", "S"),
        "SKU-LINER15": Sku("SKU-LINER15", "MAT-1005", "Knitted liner G15, no coating (sample)", "LINER", None, "15", "S"),
    }
    if seed_defects:
        skus["SKU-NBR15B"] = Sku("SKU-NBR15B", "MAT-1006", "Nitrile G15 variant (sample, no PK dipping rate)", "NITRILE", "NBR", "15", "S")
        skus["SKU-FOAM13"] = Sku("SKU-FOAM13", "MAT-1007", "Foam coated G13 (sample, no cure rule)", "FOAM", "FOAM", "13", "S")
    routings = []
    for s in skus.values():
        if s.sku == "SKU-LINER15":
            routings += [RoutingOp(s.sku, None, 10, "knitting", None, D(2), D(0), D("0.99"), D(0)),
                         RoutingOp(s.sku, None, 20, "packing", "knitting", D(0), D(1), D("0.995"), D(2))]
        else:
            routings += [RoutingOp(s.sku, None, 10, "knitting", None, D(2), D(0), D("0.99"), D(0)),
                         RoutingOp(s.sku, None, 20, "dipping", "knitting", D(1), D(2), D("0.97"), D(4)),
                         RoutingOp(s.sku, None, 30, "packing", "dipping", D(0), D(1), D("0.995"), D(2))]
    # ---- rates (dozens / machine-hour), SYNTHETIC ----
    base = {"knitting": 11, "dipping": 34, "packing": 70}
    spd = {"K1": 1.15, "K2": 1.0, "K3": 0.9, "K4": 1.05, "K5": 0.95, "K6": 1.1, "D1": 1.1, "D2": 0.95, "P1": 1.05, "P2": 0.95}
    plant_k = {"PK": 1.0, "BD": 0.92, "LK": 0.88}
    gauge_k = {"13": 1.0, "15": 0.9, "18": 0.75}
    rates, versions = [], []
    for s in skus.values():
        for pid, pl in plants.items():
            pc = pid.split("-")[0]
            for proc in ("knitting", "dipping", "packing"):
                if s.sku == "SKU-LINER15" and proc == "dipping":
                    continue
                recipe = f"R-{s.sku[4:]}-{proc[:3].upper()}"
                versions.append(ProductionVersion(s.sku, pid, proc, recipe, plan_date - timedelta(days=400)))
                for r in res.values():
                    if r.plant != pid or r.process != proc:
                        continue
                    if proc == "knitting" and (r.gauge != s.gauge):
                        continue
                    if seed_defects and s.sku == "SKU-NBR15B" and pid == "PK-1" and proc == "dipping":
                        continue
                    key = r.id.split("-")[1]
                    v = base[proc] * spd[key] * plant_k[pc] * (gauge_k[s.gauge] if proc == "knitting" else 1)
                    basis = RateBasis.GROSS if (r.id.endswith(("K3", "K6", "D2", "P2"))) else RateBasis.NET
                    status = RateValidation.VALIDATED
                    if seed_defects and r.id == "BD-D2":
                        status = RateValidation.UNVALIDATED
                    mk = lambda kind, val, b=basis, st=status: ProductionRate(
                        s.sku, pid, proc, r.id, recipe, kind, D(str(round(val, 2))), "dozens/machine-hour", b,
                        f"{SEED} {kind.value}", st, plan_date - timedelta(days=400))
                    rates.append(mk(RateKind.DEMONSTRATED, v))
                    rates.append(mk(RateKind.SAP_STANDARD, v * 1.25, RateBasis.GROSS, RateValidation.VALIDATED))
    # ---- settings (core) ----
    st = SettingsStore(clock=lambda: now)
    eff0 = plan_date - timedelta(days=400)
    for pid in plants:
        for proc, f in (("knitting", "0.88"), ("dipping", "0.85"), ("packing", "0.90")):
            if seed_defects and pid == "BD-1" and proc == "dipping":
                continue          # missing -> gross rates on BD dipping are blocked until configured
            st.put_efficiency_factor(SEED, EfficiencyFactor(Scope.PLANT_PROCESS, D(f), eff0, SEED,
                                                            Approval.APPROVED, plant=pid, process=proc), "sample seed")
    st.put_efficiency_factor(SEED, EfficiencyFactor(Scope.RESOURCE, D("0.80"), eff0, SEED, Approval.APPROVED,
                                                    plant="PK-1", resource="PK-K3"), "sample seed: K3 worse")
    for coat, h in (("NBR", "6"), ("PU", "4"), ("LATEX", "8")):
        st.put_cure_rule(SEED, CureRule(D(h), eff0, SEED, Approval.APPROVED, coating=coat), "sample seed")
    for src, crit, dec in (("sales_orders", True, ("order prioritisation", "due-date feasibility")),
                           ("inventory", True, ("material availability", "WIP/FG netting")),
                           ("capacity_calendars", True, ("finite scheduling", "utilisation")),
                           ("production_rates", False, ())):
        st.put_freshness_policy(SEED, FreshnessPolicy(src, D(24), critical=crit, affected_decisions=dec,
                                                      acknowledgment_permitted=(src != "capacity_calendars")),
                                "sample seed")
    conv = UomConverter({s.material: {"DZ": D(12)} for s in skus.values()})
    # ---- orders (pairs unless uom DZ) ----
    custs = ["CUST-A", "CUST-B", "CUST-C", "CUST-D"]
    sku_list = [k for k in skus if k not in ("SKU-NBR15B", "SKU-FOAM13")]
    orders = []
    n = 0
    for i in range(26):
        sku = sku_list[i % len(sku_list)]
        plant = ["PK-1", "PK-1", "BD-1", "LK-1"][i % 4]
        qty = D(4000 + (i * 3100) % 16000)
        due = 8 + (i * 5) % 34
        orders.append(Order(f"SO{5000 + i}", "10", custs[i % 4], sku, qty, "PR", D(0) if i % 5 else qty * D("0.2"),
                            qty if i % 3 else D(0), d(due - 3), d(due) if i % 3 else None,
                            1 + (i % 3), 1 + (i % 2), plants[plant].country, plant))
    orders.append(Order("SO5100", "10", "CUST-A", "SKU-NBR13", D(500), "DZ", D(0), D(500), d(20), d(20), 2, 1, "Pakistan", "PK-1"))
    orders.append(Order("SO5101", "10", "CUST-B", "SKU-PU18", D(10000), "PR", D(10000), D(10000), d(-5), d(-5), 2, 1, "Pakistan", "PK-1", "closed"))
    if seed_defects:
        orders.append(Order("SO5102", "10", "CUST-C", "SKU-NBR15B", D(8000), "PR", D(0), D(8000), d(25), d(25), 2, 1, "Pakistan", "PK-1"))
        orders.append(Order("SO5103", "10", "CUST-C", "SKU-FOAM13", D(6000), "PR", D(0), D(6000), d(25), d(25), 2, 1, "Pakistan", "PK-1"))
        orders.append(Order("SO5104", "10", "CUST-D", "SKU-GHOST", D(4000), "PR", D(0), D(4000), d(25), d(25), 3, 2, "Pakistan", "PK-1"))
    stocks = [Stock("SKU-NBR13", "PK-1", "FG", D(3000), D(500)),
              Stock("SKU-NBR13", "PK-1", "DIP_WIP", D(4000), D(0), D(1000)),
              Stock("SKU-PU18", "PK-1", "KNIT_WIP", D(5000)),
              Stock("SKU-LINER15", "BD-1", "FG", D(2000))]
    raw = [RawStock(f"YARN-{g}", p, "KG", D(q)) for p, q in (("PK-1", 6000), ("BD-1", 2600), ("LK-1", 4000))
           for g in ("13", "15", "18")]
    raw[0].qty = D(1200)   # PK yarn G13: short, receipt expected (demonstrates delay rule)
    receipts = [Receipt("YARN-13", "PK-1", d(9), D(9000)), Receipt("YARN-15", "BD-1", d(14), D(3000))]
    boms = [BomLine(s.sku, f"YARN-{s.gauge}", D("0.045"), "KG") for s in skus.values()]
    if seed_defects:
        boms[-1] = BomLine("SKU-FOAM13", "YARN-13", D("0.045"), "LB")   # unit mismatch vs stock (KG)
    chg = {"knitting": ChangeoverRule("knitting", D("0.5"), D("1.5"), D(0), D("2.5")),
           "dipping": ChangeoverRule("dipping", D("0.5"), D("1.0"), D("3.0"), D(0)),
           "packing": ChangeoverRule("packing", D("0.25"), D("0.5"))}
    ago = lambda h: now - timedelta(hours=h)
    refresh = [RefreshInfo("sales_orders", Origin.SAMPLE.value, ago(2), True, "sample-orders.generated", rows=len(orders)),
               RefreshInfo("inventory", Origin.SAMPLE.value, ago(30), True, "sample-stock.generated"),
               RefreshInfo("capacity_calendars", Origin.SAMPLE.value, ago(5), True, "sample-calendars.generated"),
               RefreshInfo("production_rates", Origin.SAMPLE.value, ago(60), True, "sample-rates.generated"),
               RefreshInfo("production_rates", Origin.SAMPLE.value, ago(1), False, None, ("sample: simulated refresh failure",))]
    return PlanInput(plan_date, plants, res, skus, routings, versions, rates, orders, stocks, raw, receipts, boms,
                     {"YARN-15": MaterialRule("YARN-15", "block")}, chg, PlannerConfig(), st, conv, refresh)
