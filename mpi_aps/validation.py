"""Data-quality validation. Reports affected records and an action; NEVER repairs data automatically."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from mpi_aps_core.issues import Issue
from mpi_aps_core.rates import RateBasis, RateKind, RateValidation
from mpi_aps_core.units import parse_rate_unit

from .model import PROCESSES, PlanInput
from .planner import Ctx, open_pairs


def _f(check, sev, code, msg, records, action):
    return {"check": check, "severity": sev, "code": code, "message": msg, "records": list(records), "action": action}


def _overlap(a, b):
    ae, be = a.effective_to or date.max, b.effective_to or date.max
    return a.effective_from <= be and b.effective_from <= ae


def validate(inp: PlanInput) -> list[dict]:
    out, ctx, pd = [], Ctx(inp), inp.plan_date

    # --- duplicate business keys
    for name, keys in (("orders (SO, line)", [o.key for o in inp.orders]),
                       ("routing (SKU, plant, seq)", [(r.sku, r.plant, r.seq) for r in inp.routings]),
                       ("production versions", [(v.sku, v.plant, v.process, v.recipe, v.valid_from) for v in inp.versions]),
                       ("stock (SKU, plant, stage)", [(s.sku, s.plant, s.stage) for s in inp.stocks]),
                       ("raw stock (material, plant)", [(s.material, s.plant) for s in inp.raw_stocks])):
        dups = [k for k, n in Counter(keys).items() if n > 1]
        if dups:
            out.append(_f("duplicate_keys", "error", "DUPLICATE_KEY", f"Duplicate business keys in {name}.", map(str, dups),
                          "Remove or merge the duplicate source rows; the planner does not choose between them."))
    by = defaultdict(list)
    for r in inp.rates:
        by[r.business_key()].append(r)
    for k, rs in by.items():
        if any(_overlap(a, b) for i, a in enumerate(rs) for b in rs[i + 1:]):
            out.append(_f("duplicate_keys", "error", "RATE_DUPLICATE", f"Overlapping rates for {k[:5]} {k[5].value}.", [str(k[:5])],
                          "End-date one rate or correct the effective dates."))

    # --- SKU / routing / mapping
    open_orders = [o for o in inp.orders if o.status == "open"]
    for o in open_orders:
        if o.sku not in inp.skus:
            out.append(_f("sku_mapping", "error", "SKU_UNKNOWN", f"Order SKU {o.sku} is not in the material master.", [o.key],
                          "Load the SKU into the master data or correct the order; no similar SKU is substituted."))
    pairs = {(o.sku, o.plant) for o in open_orders if o.sku in inp.skus}
    for sku, plant in sorted(pairs):
        route = ctx.route(sku, plant)
        if not route:
            out.append(_f("sku_mapping", "error", "ROUTING_MISSING", f"No routing for {sku} at {plant}.", [f"{sku}@{plant}"],
                          "Maintain the routing (operation sequence) for this SKU/plant."))
            continue
        procs = [r.process for r in route]
        for i, r in enumerate(route):
            exp = procs[i - 1] if i else None
            if r.process not in PROCESSES:
                out.append(_f("recipe_sequence", "error", "UNKNOWN_PROCESS", f"{sku}: process {r.process} is not supported.", [sku], "Fix routing."))
            if r.predecessor != exp:
                out.append(_f("recipe_sequence", "error", "BROKEN_SEQUENCE",
                              f"{sku}@{plant}: {r.process} predecessor is {r.predecessor!r}, route order implies {exp!r}.", [f"{sku}@{plant}"],
                              "Correct the predecessor or operation sequence."))
            if not (0 < r.yield_pct <= 1):
                out.append(_f("units", "error", "INVALID_YIELD", f"{sku}/{r.process} yield {r.yield_pct} not in (0,1].", [sku], "Correct the yield."))
        if len({r.seq for r in route}) != len(route):
            out.append(_f("recipe_sequence", "error", "DUPLICATE_SEQ", f"{sku}@{plant}: duplicate operation numbers.", [sku], "Renumber operations."))
        for r in route:
            cands, rej = ctx.candidates(sku, plant, r.process)
            if not cands:
                reasons = sorted({x["reason"] for x in rej})
                code = "NO_PRODUCTION_VERSION" if "NO_PRODUCTION_VERSION" in reasons else "NO_ELIGIBLE_RESOURCE"
                out.append(_f("sku_mapping", "error", code, f"{sku} at {plant}: no eligible {r.process} resource ({', '.join(reasons)}).",
                              [f"{sku}@{plant}/{r.process}"],
                              "Maintain a validated demonstrated rate for at least one compatible resource, or a production version."))
            if r.process == "dipping":
                rule, iss = inp.settings.resolve_cure_rule(plant, sku, None, inp.skus[sku].coating, pd)
                if rule is None:
                    out.append(_f("recipe_sequence", "error", iss[0].code, f"{sku}@{plant}: {iss[0].message}", [f"{sku}@{plant}"],
                                  "Maintain an approved cure/processing rule for the coating or SKU (settings)."))

    have_bom = {b.sku for b in inp.boms}
    for sku in sorted({s for s, _ in pairs}):
        if sku not in have_bom and any(r.process == "knitting" for r in inp.routings if r.sku == sku):
            out.append(_f("units", "warning", "BOM_MISSING", f"{sku} has no BOM: material constraints are NOT applied to its knitting.", [sku],
                          "Load the BOM (components and quantity per pair)."))

    # --- recipes / eligibility / rates
    rate_recipes = {(r.sku, r.plant, r.process, r.recipe) for r in inp.rates}
    for v in inp.versions:
        if (v.sku, v.plant, v.process, v.recipe) not in rate_recipes:
            out.append(_f("recipe", "warning", "RECIPE_WITHOUT_RATES", f"Recipe {v.recipe} ({v.sku}@{v.plant}/{v.process}) has no rates.", [v.recipe],
                          "Load rates for the resources that run this recipe, or retire the version."))
        if v.valid_to and v.valid_to < pd:
            out.append(_f("recipe", "warning", "RECIPE_EXPIRED", f"Recipe {v.recipe} expired {v.valid_to}.", [v.recipe], "Extend validity or remove."))
    versions = {(v.sku, v.plant, v.process, v.recipe) for v in inp.versions}
    for r in inp.rates:
        rid = f"{r.sku}/{r.plant}/{r.resource}/{r.recipe}"
        res = inp.resources.get(r.resource)
        sk = inp.skus.get(r.sku)
        if res is None:
            out.append(_f("eligibility", "error", "RATE_RESOURCE_UNKNOWN", f"Rate references unknown resource {r.resource}.", [rid], "Correct the resource id; similar names are not matched."))
            continue
        if res.plant != r.plant or res.process != r.process:
            out.append(_f("eligibility", "error", "RATE_RESOURCE_MISMATCH", f"{r.resource} belongs to {res.plant}/{res.process}, rate says {r.plant}/{r.process}.", [rid], "Correct the mapping."))
        if (r.sku, r.plant, r.process, r.recipe) not in versions:
            out.append(_f("recipe", "error", "RATE_WITHOUT_VERSION", f"Rate for recipe {r.recipe} has no production version.", [rid], "Create the production version or remove the rate."))
        if r.process == "knitting" and sk and ((sk.gauge and res.gauge != sk.gauge) or (sk.bed and res.bed != sk.bed)):
            out.append(_f("eligibility", "error", "GAUGE_BED_MISMATCH", f"{r.sku} (G{sk.gauge}/{sk.bed}) mapped to {res.id} (G{res.gauge}/{res.bed}).", [rid], "Remove the mapping or correct gauge/bed master data."))
        u, ui = parse_rate_unit(r.unit)
        if u is None:
            out.append(_f("units", "error", ui[0].code, ui[0].message, [rid], "Correct the rate unit."))
        if r.value <= 0:
            out.append(_f("rates", "error", "RATE_NOT_POSITIVE", f"Rate {r.value} <= 0.", [rid], "Correct the rate."))
        if r.kind.value == inp.config.rate_kind:
            if r.validation_status != RateValidation.VALIDATED:
                out.append(_f("rates", "warning", "RATE_UNVALIDATED", f"Planning-basis rate is {r.validation_status.value}; not used.", [rid], "Validate or reject the rate."))
            if r.basis == RateBasis.UNKNOWN:
                out.append(_f("rates", "error", "RATE_BASIS_UNKNOWN", "Gross/net basis not confirmed by source; rate not used.", [rid], "Confirm basis with the data owner."))
            if r.basis == RateBasis.GROSS and inp.settings.resolve_efficiency(r.plant, r.process, r.resource, pd).value is None:
                out.append(_f("rates", "error", "MISSING_EFFICIENCY", f"Gross rate on {r.resource} has no approved efficiency factor.", [rid],
                              "Configure a factor (resource/plant+process/plant) or have an authorised planner approve an explicit fallback."))
        if r.kind == RateKind.DEMONSTRATED:
            sap = next((x for x in by.get((r.sku, r.plant, r.process, r.resource, r.recipe, RateKind.SAP_STANDARD), [])), None)
            if sap and r.basis == sap.basis and r.value > sap.value:
                out.append(_f("rates", "warning", "DEMONSTRATED_EXCEEDS_SAP", f"Demonstrated {r.value} exceeds SAP standard {sap.value}.", [rid], "Check the SAP standard or the demonstrated data."))

    # --- units / quantities
    raw_units = {(s.material, s.plant): s.unit for s in inp.raw_stocks}
    for b in inp.boms:
        for p in {pl for (sku, pl) in pairs if sku == b.sku}:
            u = raw_units.get((b.component, p))
            if u is not None and u != b.unit:
                out.append(_f("units", "error", "BOM_STOCK_UNIT_MISMATCH", f"BOM {b.sku}/{b.component} in {b.unit}, stock at {p} in {u}.", [f"{b.sku}/{b.component}@{p}"],
                              "Provide an approved unit conversion; none is assumed."))
    for o in inp.orders:
        sk = inp.skus.get(o.sku)
        if min(o.qty, o.delivered_qty, o.confirmed_qty) < 0:
            out.append(_f("quantities", "error", "NEGATIVE_QUANTITY", "Negative quantity on order.", [o.key], "Correct the source quantity."))
        if o.delivered_qty > o.qty:
            out.append(_f("quantities", "warning", "OVER_DELIVERED", f"Delivered {o.delivered_qty} > ordered {o.qty}.", [o.key], "Verify delivery posting."))
        if o.confirmed_qty > o.qty:
            out.append(_f("quantities", "warning", "OVER_CONFIRMED", f"Confirmed {o.confirmed_qty} > ordered {o.qty}.", [o.key], "Verify schedule lines."))
        if o.status == "open" and sk:
            _, iss = open_pairs(inp, o)
            for i in iss:
                out.append(_f("units", "error", i.code, i.message, [o.key], "Maintain the material-specific unit conversion."))
        if o.plant not in inp.plants:
            out.append(_f("sku_mapping", "error", "PLANT_UNKNOWN", f"Order plant {o.plant} unknown.", [o.key], "Correct the plant."))
    for s in inp.stocks:
        if min(s.qty, s.allocated, s.blocked) < 0 or s.allocated + s.blocked > s.qty:
            out.append(_f("quantities", "error", "IMPOSSIBLE_STOCK", f"{s.sku}@{s.plant}/{s.stage}: allocated+blocked exceeds stock or negative.", [f"{s.sku}@{s.plant}/{s.stage}"], "Correct inventory source."))
    for r in inp.resources.values():
        if r.plant not in inp.plants:
            out.append(_f("calendar", "error", "RESOURCE_PLANT_UNKNOWN", f"Resource {r.id} references unknown plant {r.plant}.", [r.id], "Load the plant (with calendar) or correct the resource."))
            continue
        sp = r.calendar or inp.plants[r.plant].calendar
        if not sp.shifts or not sp.working_weekdays:
            out.append(_f("calendar", "error", "NO_WORKING_TIME", f"{r.id} has no shifts/working days.", [r.id], "Maintain the calendar."))
    merged: dict = {}
    for f in out:                                   # one finding per (code, message); affected records combined
        k = (f["code"], f["message"])
        if k in merged:
            merged[k]["records"] += [r for r in f["records"] if r not in merged[k]["records"]]
        else:
            merged[k] = f
    return list(merged.values())


def summarise(findings: list[dict]) -> dict:
    c = Counter((f["check"], f["severity"]) for f in findings)
    return {"total": len(findings), "errors": sum(1 for f in findings if f["severity"] == "error"),
            "warnings": sum(1 for f in findings if f["severity"] == "warning"),
            "by_check": {f"{k[0]}:{k[1]}": v for k, v in sorted(c.items())}}


def mapping_matrix(inp: PlanInput) -> list[dict]:
    ctx, rows = Ctx(inp), []
    plants = sorted(inp.plants)
    for sku in sorted(inp.skus):
        for p in plants:
            route = ctx.route(sku, p)
            for r in route:
                cands, rej = ctx.candidates(sku, p, r.process)
                rows.append({"sku": sku, "plant": p, "process": r.process, "status": "ok" if cands else "missing",
                             "eligible": [{"resource": c.resource, "recipe": c.recipe, "rate": str(c.rate.value), "unit": c.rate.unit,
                                           "basis": c.rate.basis.value, "validation": c.rate.validation_status.value,
                                           "source": c.rate.source} for c in cands],
                             "rejected": rej})
    return rows


def rate_comparison(inp: PlanInput) -> list[dict]:
    g = defaultdict(dict)
    for r in inp.rates:
        g[(r.sku, r.plant, r.process, r.resource, r.recipe)][r.kind.value] = r
    out = []
    for k, d in sorted(g.items()):
        dem, sap = d.get("demonstrated"), d.get("sap_standard")
        out.append({"sku": k[0], "plant": k[1], "process": k[2], "resource": k[3], "recipe": k[4],
                    "sap_standard": str(sap.value) if sap else None, "demonstrated": str(dem.value) if dem else None,
                    "planned": str(d["planned"].value) if d.get("planned") else None, "unit": (dem or sap).unit,
                    "demonstrated_vs_sap_pct": round(float((dem.value / sap.value - 1) * 100), 1) if dem and sap and sap.value else None,
                    "basis": dem.basis.value if dem else None, "validation": dem.validation_status.value if dem else None,
                    "actual": "no execution data connected"})
    return out
