"""Master-data CSV import/export (replace-by-entity, validated, strict by default).

Each entity is loaded as a COMPLETE snapshot that replaces the previous (sample or uploaded) set for that entity.
Rows are validated for required fields, types, enums and duplicate keys. Cross-entity references (e.g. a rate pointing
at a resource) are checked afterwards by `validation.validate`, which reports - never repairs - inconsistencies.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from mpi_aps_core.rates import ProductionRate, RateBasis, RateKind, RateValidation
from mpi_aps_core.units import parse_rate_unit

from .model import (BomLine, CalendarSpec, ChangeoverRule, Downtime, MaterialRule, Plant, PROCESSES, ProductionVersion,
                    RawStock, Receipt, Resource, RoutingOp, Sku, Stock)

SOURCE_OF = {"plants": "capacity_calendars", "resources": "capacity_calendars", "downtimes": "capacity_calendars",
             "skus": "master_data", "routings": "master_data", "versions": "master_data", "boms": "master_data",
             "changeovers": "master_data", "material_rules": "master_data", "rates": "production_rates",
             "stocks": "inventory", "raw_stocks": "inventory", "receipts": "inventory"}


def _s(r, c):
    v = (r.get(c) or "").strip()
    return v or None


def _req(r, c):
    v = _s(r, c)
    if v is None:
        raise ValueError(f"{c} is required")
    return v


def _dec(r, c, positive=False, required=True, default=None):
    v = _s(r, c)
    if v is None:
        if required and default is None:
            raise ValueError(f"{c} is required")
        return Decimal(default) if default is not None else None
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{c} '{v}' is not a number")
    if d < 0 or (positive and d == 0):
        raise ValueError(f"{c} must be {'> 0' if positive else '>= 0'}")
    return d


def _date(r, c, required=True):
    v = _s(r, c)
    if v is None:
        if required:
            raise ValueError(f"{c} is required")
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{c} '{v}' must be ISO yyyy-mm-dd")


def _dt(r, c):
    try:
        return datetime.fromisoformat(_req(r, c))
    except ValueError:
        raise ValueError(f"{c} must be ISO yyyy-mm-ddThh:mm")


def _enum(r, c, allowed, required=True, default=None):
    v = (_s(r, c) or default)
    if v is None:
        if required:
            raise ValueError(f"{c} is required")
        return None
    v = v.lower() if all(a.islower() or a.isdigit() or a in "_-" for a in allowed) else v
    if v not in allowed:
        raise ValueError(f"{c} '{v}' must be one of {sorted(allowed)}")
    return v


def _bool(r, c, default=True):
    v = (_s(r, c) or ("Y" if default else "N")).upper()
    if v not in ("Y", "N"):
        raise ValueError(f"{c} must be Y or N")
    return v == "Y"


def _shifts(txt):
    out = []
    for part in txt.split(";"):
        a, b = part.split("-")
        a, b = float(a), float(b)
        if not (0 <= a < b <= 24):
            raise ValueError
        out.append((a, b))
    out.sort()
    if any(x[1] > y[0] for x, y in zip(out, out[1:])):
        raise ValueError
    return out


def _plant(r):
    try:
        shifts = _shifts(_req(r, "shifts"))
    except ValueError:
        raise ValueError("shifts must look like 6-14;14-22 (non-overlapping hours within 0-24)")
    try:
        wd = frozenset(int(x) for x in _req(r, "working_weekdays").split(","))
        if not wd <= set(range(7)):
            raise ValueError
    except ValueError:
        raise ValueError("working_weekdays must be comma-separated 0-6 (Mon=0)")
    try:
        hol = frozenset(date.fromisoformat(x) for x in (_s(r, "holidays") or "").split(";") if x)
    except ValueError:
        raise ValueError("holidays must be ISO dates separated by ;")
    return Plant(_req(r, "plant"), _req(r, "country"), _req(r, "name"), CalendarSpec(shifts, wd, hol), "upload")


def _rate(r):
    unit = _req(r, "unit")
    u, iss = parse_rate_unit(unit)
    if u is None:
        raise ValueError(iss[0].message)
    return ProductionRate(_req(r, "sku"), _req(r, "plant"), _enum(r, "process", PROCESSES), _req(r, "resource"), _req(r, "recipe"),
                          RateKind(_enum(r, "kind", {k.value for k in RateKind})), _dec(r, "value", positive=True), unit,
                          RateBasis(_enum(r, "basis", {k.value for k in RateBasis}, default="unknown")), _req(r, "source"),
                          RateValidation(_enum(r, "validation_status", {k.value for k in RateValidation}, default="unvalidated")),
                          _date(r, "effective_from"), _date(r, "effective_to", False), _bool(r, "includes_setup", False))


def _routing(r):
    y = _dec(r, "yield_pct", positive=True, default="1")
    if y > 1:
        raise ValueError("yield_pct must be in (0,1]")
    return RoutingOp(_req(r, "sku"), _s(r, "plant"), int(_req(r, "seq")), _enum(r, "process", PROCESSES), _s(r, "predecessor"),
                     _dec(r, "transfer_hours", default="0"), _dec(r, "queue_hours", default="0"), y,
                     _dec(r, "qa_checkpoint_hours", default="0"), "upload")


# entity -> (columns, key columns, builder)
SPECS = {
    "plants": (["plant", "country", "name", "shifts", "working_weekdays", "holidays"], ["plant"], _plant),
    "resources": (["resource", "plant", "process", "group", "description", "gauge", "bed", "active"], ["resource"],
                  lambda r: Resource(_req(r, "resource"), _req(r, "plant"), _enum(r, "process", PROCESSES), _req(r, "group"),
                                     _req(r, "description"), _s(r, "gauge"), _s(r, "bed"), _bool(r, "active"), origin="upload")),
    "downtimes": (["resource", "start", "end", "kind", "reason"], ["resource", "start"],
                  lambda r: (_req(r, "resource"), Downtime(_dt(r, "start"), _dt(r, "end"), _enum(r, "kind", {"maintenance", "breakdown"}), _s(r, "reason") or ""))),
    "skus": (["sku", "material", "description", "family", "coating", "gauge", "bed", "customer_pack"], ["sku"],
             lambda r: Sku(_req(r, "sku"), _req(r, "material"), _req(r, "description"), _req(r, "family"), _s(r, "coating"),
                           _s(r, "gauge"), _s(r, "bed"), _s(r, "customer_pack"), "PR", "upload")),
    "routings": (["sku", "plant", "seq", "process", "predecessor", "transfer_hours", "queue_hours", "yield_pct", "qa_checkpoint_hours"],
                 ["sku", "plant", "seq"], _routing),
    "versions": (["sku", "plant", "process", "recipe", "valid_from", "valid_to"], ["sku", "plant", "process", "recipe", "valid_from"],
                 lambda r: ProductionVersion(_req(r, "sku"), _req(r, "plant"), _enum(r, "process", PROCESSES), _req(r, "recipe"),
                                             _date(r, "valid_from"), _date(r, "valid_to", False), "upload")),
    "rates": (["sku", "plant", "process", "resource", "recipe", "kind", "value", "unit", "basis", "source", "validation_status",
               "effective_from", "effective_to", "includes_setup"], ["sku", "plant", "process", "resource", "recipe", "kind", "effective_from"], _rate),
    "stocks": (["sku", "plant", "stage", "qty", "allocated", "blocked"], ["sku", "plant", "stage"],
               lambda r: Stock(_req(r, "sku"), _req(r, "plant"), _enum(r, "stage", {"FG", "DIP_WIP", "KNIT_WIP"}), _dec(r, "qty"),
                               _dec(r, "allocated", default="0"), _dec(r, "blocked", default="0"), "upload")),
    "raw_stocks": (["material", "plant", "unit", "qty", "reserved", "blocked"], ["material", "plant"],
                   lambda r: RawStock(_req(r, "material"), _req(r, "plant"), _req(r, "unit"), _dec(r, "qty"),
                                      _dec(r, "reserved", default="0"), _dec(r, "blocked", default="0"), "upload")),
    "receipts": (["material", "plant", "date", "qty"], ["material", "plant", "date"],
                 lambda r: Receipt(_req(r, "material"), _req(r, "plant"), _date(r, "date"), _dec(r, "qty", positive=True), "upload")),
    "boms": (["sku", "component", "qty_per_pair", "unit"], ["sku", "component"],
             lambda r: BomLine(_req(r, "sku"), _req(r, "component"), _dec(r, "qty_per_pair", positive=True), _req(r, "unit"), "upload")),
    "changeovers": (["process", "sku_change_h", "family_change_h", "coating_change_h", "gauge_change_h", "source"], ["process"],
                    lambda r: ChangeoverRule(_enum(r, "process", PROCESSES), _dec(r, "sku_change_h"), _dec(r, "family_change_h"),
                                             _dec(r, "coating_change_h", default="0"), _dec(r, "gauge_change_h", default="0"), _req(r, "source"))),
    "material_rules": (["material", "on_shortage"], ["material"],
                       lambda r: MaterialRule(_req(r, "material"), _enum(r, "on_shortage", {"delay", "block"}))),
}


def template(entity: str) -> str:
    return ",".join(SPECS[entity][0]) + "\n"


def parse_rows(entity: str, rows: list[dict]):
    cols, keys, build = SPECS[entity]
    objs, rejected, seen = [], [], set()
    for n, r in enumerate(rows, start=2):
        try:
            key = tuple((r.get(k) or "").strip() for k in keys)
            if key in seen:
                raise ValueError(f"duplicate key {key}")
            obj = build(r)
            seen.add(key)
            objs.append((obj, r))
        except (ValueError, KeyError, TypeError) as e:
            rejected.append({"row": n, "key": "/".join((r.get(k) or "").strip() for k in keys), "errors": [str(e)]})
    return objs, rejected


def parse_csv(entity: str, text: str):
    if entity not in SPECS:
        raise ValueError(f"Unknown entity '{entity}'. Valid: {', '.join(sorted(SPECS))}")
    rd = csv.DictReader(io.StringIO(text))
    cols = SPECS[entity][0]
    missing = [c for c in cols if c not in (rd.fieldnames or [])]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    rows = list(rd)
    objs, rej = parse_rows(entity, rows)
    return rows, objs, rej


def apply(inp, entity: str, objs: list) -> None:
    """Replace the entity's records in a PlanInput with parsed objects."""
    o = [x[0] for x in objs]
    if entity == "plants":
        inp.plants = {p.id: p for p in o}
    elif entity == "resources":
        inp.resources = {r.id: r for r in o}
    elif entity == "downtimes":
        for r in inp.resources.values():
            r.downtimes = [d for d in r.downtimes if False]
        for rid, d in o:
            if rid in inp.resources:
                inp.resources[rid].downtimes.append(d)
    elif entity == "skus":
        inp.skus = {s.sku: s for s in o}
    elif entity == "changeovers":
        inp.changeovers = {c.process: c for c in o}
    elif entity == "material_rules":
        inp.material_rules = {m.material: m for m in o}
    else:
        setattr(inp, {"routings": "routings", "versions": "versions", "rates": "rates", "stocks": "stocks",
                      "raw_stocks": "raw_stocks", "receipts": "receipts", "boms": "boms"}[entity], o)


def _f(x):
    return "" if x is None else (f"{x:g}" if isinstance(x, float) else str(x))


def to_rows(entity: str, inp) -> list[dict]:
    if entity == "plants":
        return [{"plant": p.id, "country": p.country, "name": p.name, "shifts": ";".join(f"{a:g}-{b:g}" for a, b in p.calendar.shifts),
                 "working_weekdays": ",".join(map(str, sorted(p.calendar.working_weekdays))),
                 "holidays": ";".join(d.isoformat() for d in sorted(p.calendar.holidays))} for p in inp.plants.values()]
    if entity == "resources":
        return [{"resource": r.id, "plant": r.plant, "process": r.process, "group": r.group, "description": r.description,
                 "gauge": _f(r.gauge), "bed": _f(r.bed), "active": "Y" if r.active else "N"} for r in inp.resources.values()]
    if entity == "downtimes":
        return [{"resource": r.id, "start": d.start.isoformat(timespec="minutes"), "end": d.end.isoformat(timespec="minutes"),
                 "kind": d.kind, "reason": d.reason} for r in inp.resources.values() for d in r.downtimes]
    if entity == "skus":
        return [{"sku": s.sku, "material": s.material, "description": s.description, "family": s.family, "coating": _f(s.coating),
                 "gauge": _f(s.gauge), "bed": _f(s.bed), "customer_pack": _f(s.customer_pack)} for s in inp.skus.values()]
    if entity == "routings":
        return [{"sku": r.sku, "plant": _f(r.plant), "seq": r.seq, "process": r.process, "predecessor": _f(r.predecessor),
                 "transfer_hours": r.transfer_hours, "queue_hours": r.queue_hours, "yield_pct": r.yield_pct,
                 "qa_checkpoint_hours": r.qa_checkpoint_hours} for r in inp.routings]
    if entity == "versions":
        return [{"sku": v.sku, "plant": v.plant, "process": v.process, "recipe": v.recipe, "valid_from": v.valid_from.isoformat(),
                 "valid_to": v.valid_to.isoformat() if v.valid_to else ""} for v in inp.versions]
    if entity == "rates":
        return [{"sku": r.sku, "plant": r.plant, "process": r.process, "resource": r.resource, "recipe": r.recipe, "kind": r.kind.value,
                 "value": r.value, "unit": r.unit, "basis": r.basis.value, "source": r.source, "validation_status": r.validation_status.value,
                 "effective_from": r.effective_from.isoformat(), "effective_to": r.effective_to.isoformat() if r.effective_to else "",
                 "includes_setup": "Y" if r.includes_setup else "N"} for r in inp.rates]
    if entity == "stocks":
        return [{"sku": s.sku, "plant": s.plant, "stage": s.stage, "qty": s.qty, "allocated": s.allocated, "blocked": s.blocked} for s in inp.stocks]
    if entity == "raw_stocks":
        return [{"material": s.material, "plant": s.plant, "unit": s.unit, "qty": s.qty, "reserved": s.reserved, "blocked": s.blocked} for s in inp.raw_stocks]
    if entity == "receipts":
        return [{"material": r.material, "plant": r.plant, "date": r.on.isoformat(), "qty": r.qty} for r in inp.receipts]
    if entity == "boms":
        return [{"sku": b.sku, "component": b.component, "qty_per_pair": b.qty_per_pair, "unit": b.unit} for b in inp.boms]
    if entity == "changeovers":
        return [{"process": c.process, "sku_change_h": c.sku_change_h, "family_change_h": c.family_change_h,
                 "coating_change_h": c.coating_change_h, "gauge_change_h": c.gauge_change_h, "source": c.source} for c in inp.changeovers.values()]
    if entity == "material_rules":
        return [{"material": m.material, "on_shortage": m.on_shortage} for m in inp.material_rules.values()]
    raise ValueError(entity)


def to_csv(entity: str, inp) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=SPECS[entity][0], lineterminator="\n")
    w.writeheader()
    w.writerows(to_rows(entity, inp))
    return out.getvalue()
