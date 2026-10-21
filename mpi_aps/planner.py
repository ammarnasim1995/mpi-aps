"""Deterministic planning engine (no I/O, no AI).

Pipeline: orders -> priority -> net requirements (FG/WIP, yield) -> lots/operations -> material ledger
-> candidate (recipe, resource) selection -> finite-capacity placement -> `resolve` (recompute all dates
from assignments + per-resource sequences) -> validation.  See docs/CALCULATIONS.md and docs/SCHEDULING.md.

A schedule is DECISIONS (resource, recipe, sequence, not-before pins) + DERIVED TIMES.  Because times are
always derived, a manual change (or a change in knitting completion) recalculates dipping/packing dates and
resource loading by simply re-running `resolve`.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from mpi_aps_core.curing import QualityStatus, earliest_packing_start
from mpi_aps_core.issues import ERROR, Issue
from mpi_aps_core.rates import ProductionRate, RateKind, required_hours, select_rate

from .calendar import Segment, allocate, calendar_for
from .model import Order, PlanInput

STAGE_STOCK = {"packing": "FG", "dipping": "DIP_WIP", "knitting": "KNIT_WIP"}
HORIZON_LIMIT_DAYS = 400


class ScheduleCycle(Exception):
    pass


# ----------------------------------------------------------------------------- structures
@dataclass
class Candidate:
    recipe: str
    resource: str
    rate: ProductionRate
    fallback: bool = False


@dataclass
class Op:
    id: str
    lot_id: str
    order_key: str
    sku: str
    plant: str
    process: str
    run_qty: int
    wip_in: int = 0                                 # pairs supplied from released WIP stock into this op
    preds: list = field(default_factory=list)       # [(op_id, kind)] kind: transfer | cure
    material_ready: Optional[datetime] = None
    material_note: str = ""
    blocked: Optional[str] = None
    # decisions
    resource: Optional[str] = None
    recipe: Optional[str] = None
    not_before: Optional[datetime] = None
    manual_by: Optional[str] = None
    manual_reason: str = ""
    # derived
    start: Optional[datetime] = None
    finish: Optional[datetime] = None
    run_hours: float = 0.0
    setup_hours: float = 0.0
    segments: list = field(default_factory=list)
    driver: str = ""
    calendar_wait_h: float = 0.0
    issues: list = field(default_factory=list)
    candidates: list = field(default_factory=list)  # decision table for explainability


@dataclass
class Schedule:
    ops: dict = field(default_factory=dict)         # op_id -> Op (insertion = priority order)
    seq: dict = field(default_factory=dict)         # resource -> [op_id]
    order_notes: dict = field(default_factory=dict)  # order_key -> list[str]
    order_issues: dict = field(default_factory=dict)  # order_key -> list[Issue]
    ranks: dict = field(default_factory=dict)       # order_key -> (rank, explanation)
    reqs: list = field(default_factory=list)       # net requirements per order (priority order)

    def decisions(self) -> dict:
        return {"ops": {o.id: {"resource": o.resource, "recipe": o.recipe,
                               "not_before": o.not_before.isoformat() if o.not_before else None,
                               "manual_by": o.manual_by, "manual_reason": o.manual_reason}
                        for o in self.ops.values() if o.resource},
                "seq": {r: list(s) for r, s in self.seq.items()}}


# ----------------------------------------------------------------------------- context / lookups
class Ctx:
    """Per-run indexes and caches. Never mutates the PlanInput."""

    def __init__(self, inp: PlanInput):
        self.inp = inp
        self.rate_idx = defaultdict(list)
        for r in inp.rates:
            self.rate_idx[(r.sku, r.plant, r.process, r.resource, r.recipe, r.kind)].append(r)
        self._cand: dict = {}
        self._hours: dict = {}

    def route(self, sku: str, plant: str) -> list:
        specific = [r for r in self.inp.routings if r.sku == sku and r.plant == plant]
        generic = [r for r in self.inp.routings if r.sku == sku and r.plant is None]
        return sorted(specific or generic, key=lambda r: r.seq)

    def route_op(self, sku, plant, process):
        return next((r for r in self.route(sku, plant) if r.process == process), None)

    def candidates(self, sku: str, plant: str, process: str):
        k = (sku, plant, process)
        if k in self._cand:
            return self._cand[k]
        inp, pd = self.inp, self.inp.plan_date
        cands, rejected = [], []
        vers = [v for v in inp.versions if (v.sku, v.plant, v.process) == k and v.valid_from <= pd
                and (v.valid_to is None or pd <= v.valid_to)]
        sk = inp.skus.get(sku)
        if not vers:
            rejected.append({"reason": "NO_PRODUCTION_VERSION",
                             "message": f"No valid production version/recipe for {sku} at {plant} ({process})."})
        kind = RateKind(inp.config.rate_kind)
        for v in vers:
            for r in inp.resources.values():
                if r.plant != plant or r.process != process or not r.active:
                    continue
                if process == "knitting" and sk and ((sk.gauge and r.gauge != sk.gauge) or (sk.bed and r.bed != sk.bed)):
                    continue                                      # technically incompatible: not a candidate
                hits = self.rate_idx.get((sku, plant, process, r.id, v.recipe, kind), [])
                rate, iss = select_rate(hits, sku=sku, plant=plant, process=process, resource=r.id,
                                        recipe=v.recipe, kind=kind, as_of=pd)
                fb = False
                if rate is None and inp.config.allow_sap_standard_fallback and kind != RateKind.SAP_STANDARD:
                    rate, iss2 = select_rate(self.rate_idx.get((sku, plant, process, r.id, v.recipe, RateKind.SAP_STANDARD), []),
                                             sku=sku, plant=plant, process=process, resource=r.id, recipe=v.recipe,
                                             kind=RateKind.SAP_STANDARD, as_of=pd)
                    fb = rate is not None
                if rate is None:
                    rejected.append({"resource": r.id, "recipe": v.recipe, "reason": iss[0].code, "message": iss[0].message})
                else:
                    cands.append(Candidate(v.recipe, r.id, rate, fb))
        self._cand[k] = (cands, rejected)
        return self._cand[k]

    def hours(self, op: Op, cand: Candidate, setup_h: Decimal):
        key = (cand.rate.business_key(), cand.rate.effective_from, op.run_qty, str(setup_h))
        if key not in self._hours:
            sk = self.inp.skus[op.sku]
            self._hours[key] = required_hours(qty=Decimal(op.run_qty), qty_unit="PR", material=sk.material,
                                              rate=cand.rate, settings=self.inp.settings, converter=self.inp.converter,
                                              as_of=self.inp.plan_date, setup_hours=setup_h)
        return self._hours[key]


def changeover_hours(inp: PlanInput, process: str, prev_sku: str, next_sku: str) -> tuple[Decimal, Optional[str]]:
    if prev_sku == next_sku:
        return Decimal(0), None
    rule = inp.changeovers.get(process)
    if rule is None:
        return Decimal(0), f"No changeover rule for {process}; no changeover time can be derived."
    a, b = inp.skus[prev_sku], inp.skus[next_sku]
    h = rule.sku_change_h
    if a.family != b.family:
        h = max(h, rule.family_change_h)
    if process == "dipping" and a.coating != b.coating:
        h = max(h, rule.coating_change_h)
    if process == "knitting" and a.gauge != b.gauge:
        h = max(h, rule.gauge_change_h)
    return h, None


# ----------------------------------------------------------------------------- demand & priority
def open_pairs(inp: PlanInput, o: Order) -> tuple[Optional[int], list[Issue]]:
    if o.status != "open":
        return 0, []
    sk = inp.skus.get(o.sku)
    if sk is None:
        return None, [Issue("SKU_UNKNOWN", ERROR, f"SKU {o.sku} is not in the material master.", {"order": o.key})]
    rec = {"order": o.key, "sku": o.sku}
    vals = {}
    for name, q in (("ordered", o.qty), ("delivered", o.delivered_qty), ("confirmed", o.confirmed_qty)):
        v, iss = inp.converter.convert(sk.material, q, o.uom, "PR")
        if v is None:
            return None, [Issue(i.code, i.severity, i.message, {**rec, **i.record}) for i in iss]
        vals[name] = v
    open_q = max(Decimal(0), vals["ordered"] - vals["delivered"])
    if not inp.config.plan_unconfirmed:
        open_q = min(open_q, max(Decimal(0), vals["confirmed"] - vals["delivered"]))
    return int(open_q.to_integral_value(rounding="ROUND_CEILING")), []


def prioritise(inp: PlanInput) -> list[tuple[Order, int, str]]:
    ovr = inp.priority_overrides
    keyfn = {
        "override": lambda o: (0, ovr[o.key][0]) if o.key in ovr else (1, 0),
        "customer_priority": lambda o: o.cust_priority,
        "due_date": lambda o: o.due,
        "ship_priority": lambda o: o.ship_priority,
        "order_key": lambda o: o.key,
    }
    rules = inp.config.priority_rules
    orders = [o for o in inp.orders if o.status == "open"]
    orders.sort(key=lambda o: tuple(keyfn[r](o) for r in rules))
    out = []
    for i, o in enumerate(orders, 1):
        why = (f"planner override #{ovr[o.key][0]} by {ovr[o.key][2]}: {ovr[o.key][1]}" if o.key in ovr else
               f"rule order {' > '.join(rules)}; customer priority {o.cust_priority}, due {o.due}, ship priority {o.ship_priority}")
        out.append((o, i, why))
    return out


@dataclass
class Requirement:
    order: Order
    rank: int
    why_rank: str
    open_pairs: Optional[int]
    stages: list = field(default_factory=list)   # [(process, run_qty, wip_used)] in route order
    fg_used: int = 0
    issues: list = field(default_factory=list)


def requirements(inp: PlanInput, ctx: Ctx) -> list[Requirement]:
    avail = defaultdict(int)
    for s in inp.stocks:
        avail[(s.sku, s.plant, s.stage)] += int(s.available)
    reqs = []
    for o, rank, why in prioritise(inp):
        q, iss = open_pairs(inp, o)
        r = Requirement(o, rank, why, q, issues=list(iss))
        reqs.append(r)
        if q is None or q == 0:
            continue
        route = ctx.route(o.sku, o.plant)
        if not route:
            r.issues.append(Issue("ROUTING_MISSING", ERROR, f"No routing for {o.sku} at {o.plant}.", {"order": o.key, "sku": o.sku}))
            continue
        out = q
        stages = []
        for ro in reversed(route):
            sk_key = (o.sku, o.plant, STAGE_STOCK[ro.process])
            use = min(out, avail[sk_key])
            avail[sk_key] -= use
            if ro.process == "packing":
                r.fg_used = use
            out -= use
            if not (Decimal(0) < ro.yield_pct <= 1):
                r.issues.append(Issue("INVALID_YIELD", ERROR, f"Yield {ro.yield_pct} invalid for {o.sku}/{ro.process}.", {"order": o.key}))
                break
            run = int((Decimal(out) / ro.yield_pct).to_integral_value(rounding="ROUND_CEILING")) if out else 0
            stages.append((ro.process, run, use))
            out = run
        r.stages = list(reversed(stages))
    return reqs


def _distribute(total: int, n: int) -> list[int]:
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


# ----------------------------------------------------------------------------- build lots / ops
def build_schedule_skeleton(inp: PlanInput, ctx: Ctx) -> tuple[Schedule, list[Requirement]]:
    sched = Schedule()
    reqs = requirements(inp, ctx)
    for r in reqs:
        o = r.order
        sched.ranks[o.key] = (r.rank, r.why_rank)
        sched.order_issues[o.key] = list(r.issues)
        if not r.stages:
            continue
        n = max(1, int(inp.config.lots_by_order.get(o.key, 1)))
        per_stage = {p: (_distribute(q, n), _distribute(w, n)) for p, q, w in r.stages}
        route = ctx.route(o.sku, o.plant)
        for li in range(n):
            lot_id = f"{o.key}/L{li + 1}"
            prev: Optional[Op] = None
            for ri, ro in enumerate(route):
                qs, _ = per_stage[ro.process]
                if qs[li] <= 0:
                    continue
                win = per_stage[route[ri - 1].process][1][li] if ri > 0 else 0   # stock consumed at previous stage output
                op = Op(f"{lot_id}:{ro.process}", lot_id, o.key, o.sku, o.plant, ro.process, qs[li], win)
                if prev is not None:
                    kind = "cure" if (prev.process == "dipping" and ro.process == "packing") else "transfer"
                    op.preds.append((prev.id, kind))
                sched.ops[op.id] = op
                prev = op
    sched.reqs = reqs
    _allocate_materials(inp, ctx, sched)
    return sched, reqs


def _allocate_materials(inp: PlanInput, ctx: Ctx, sched: Schedule) -> None:
    stock = defaultdict(lambda: [Decimal(0), None])        # (material, plant) -> [available_now, unit]
    for s in inp.raw_stocks:
        k = (s.material, s.plant)
        stock[k][0] += s.available
        stock[k][1] = s.unit
    rec = defaultdict(list)
    for r in inp.receipts:
        rec[(r.material, r.plant)].append((datetime(r.on.year, r.on.month, r.on.day), r.qty))
    for v in rec.values():
        v.sort()
    cum = defaultdict(Decimal)
    for op in sched.ops.values():
        if op.process != "knitting":
            continue
        ready, notes = inp.horizon_start, []
        for b in (b for b in inp.boms if b.sku == op.sku):
            need = Decimal(op.run_qty) * b.qty_per_pair
            k = (b.component, op.plant)
            now_av, unit = stock[k]
            if unit is None and not rec[k]:
                op.blocked = f"Material {b.component} has no stock record at {op.plant}; availability unknown."
                break
            if unit is not None and unit != b.unit:
                op.blocked = (f"Unit mismatch for {b.component}: BOM in {b.unit}, stock in {unit}. "
                              "No conversion is assumed.")
                break
            rule = inp.material_rules.get(b.component)
            on_short = rule.on_shortage if rule else "delay"
            target = cum[k] + need
            if target <= now_av:
                cum[k] = target
                notes.append(f"{b.component}: {need:.1f} {b.unit} covered by available stock")
                continue
            if on_short == "block":
                op.blocked = (f"Material shortage {b.component} at {op.plant}: need {need:.1f} {b.unit}, "
                              f"{max(Decimal(0), now_av - cum[k]):.1f} available; rule 'block' prevents scheduling.")
                break
            run, when = now_av, None
            for dt, q in rec[k]:
                run += q
                if target <= run:
                    when = dt
                    break
            if when is None:
                op.blocked = (f"Material shortage {b.component} at {op.plant}: need {need:.1f} {b.unit}; "
                              "stock plus expected receipts do not cover it.")
                break
            cum[k] = target
            ready = max(ready, when)
            notes.append(f"{b.component}: shortage covered by receipt on {when.date()} (rule 'delay')")
        op.material_ready = ready
        op.material_note = "; ".join(notes)


# ----------------------------------------------------------------------------- placement
@dataclass
class Placement:
    ok: bool
    reason: str = ""
    start: Optional[datetime] = None
    finish: Optional[datetime] = None
    segs: list = field(default_factory=list)
    run_h: float = 0.0
    setup_h: float = 0.0
    driver: str = ""
    earliest: Optional[datetime] = None
    wait_h: float = 0.0
    issues: list = field(default_factory=list)
    overtime_h: float = 0.0


def _pred_ready(ctx: Ctx, sched: Schedule, op: Op):
    """Return (components dict, blocked_reason, issues) from predecessor operations."""
    inp = ctx.inp
    comps, issues = {}, []
    for pid, kind in op.preds:
        p = sched.ops[pid]
        if p.finish is None:
            return comps, f"predecessor {p.process} ({p.id}) is not scheduled: {p.blocked or 'unscheduled'}", issues
        pr = ctx.route_op(p.sku, p.plant, p.process)
        sr = ctx.route_op(op.sku, op.plant, op.process)
        lag = timedelta(hours=float((pr.transfer_hours if pr else 0) + (sr.queue_hours if sr else 0)))
        if kind == "cure":
            qa = timedelta(hours=float(pr.qa_checkpoint_hours if pr else 0))
            res = earliest_packing_start(dipping_completion=p.finish, plant=op.plant, sku=op.sku, recipe=p.recipe,
                                         coating=inp.skus[op.sku].coating, quality=QualityStatus.RELEASED,
                                         quality_released_at=p.finish + qa, settings=inp.settings, as_of=inp.plan_date)
            if res.earliest_start is None:
                issues += list(res.issues)
                return comps, f"{res.issues[0].code}: {res.issues[0].message}", issues
            comps[f"cure+quality release after {p.process}"] = res.earliest_start + lag
        else:
            comps[f"{p.process} output available"] = p.finish + lag
    return comps, None, issues


def place(ctx: Ctx, sched: Schedule, op: Op, cand: Candidate, prev: Optional[Op]) -> Placement:
    inp = ctx.inp
    comps, blocked, issues = _pred_ready(ctx, sched, op)
    if blocked:
        return Placement(False, blocked, issues=issues)
    comps["plan start"] = inp.horizon_start
    if op.material_ready is not None and op.process == "knitting":
        comps["material ready"] = op.material_ready
    if op.not_before:
        comps["planner not-before pin"] = op.not_before
    setup = Decimal(0)
    if prev is not None and prev.finish is not None:
        comps[f"resource busy until {prev.id}"] = prev.finish
        setup, warn = changeover_hours(inp, op.process, prev.sku, op.sku)
        if warn:
            return Placement(False, warn)
    earliest = max(comps.values())
    driver = max(comps, key=lambda k: comps[k])
    hr = ctx.hours(op, cand, setup)
    if not hr.ok:
        bad = next((i for i in hr.issues if i.severity == "error"), None)
        return Placement(False, f"{bad.code}: {bad.message}" if bad else "hours not computable", issues=list(hr.issues))
    res = inp.resources[cand.resource]
    total_h = float(hr.run_hours + hr.setup_hours)
    segs = allocate(calendar_for(res, inp.plants), res.downtimes, earliest, total_h, HORIZON_LIMIT_DAYS)
    if not segs:
        return Placement(False, f"No working capacity on {res.id} within {HORIZON_LIMIT_DAYS} days.")
    wait = (segs[0].start - earliest).total_seconds() / 3600
    return Placement(True, "", segs[0].start, segs[-1].end, segs, float(hr.run_hours), float(hr.setup_hours),
                     driver, earliest, wait, list(hr.issues), sum(s.hours for s in segs if s.overtime))


def _commit(op: Op, pl: Placement) -> None:
    op.start, op.finish, op.segments = pl.start, pl.finish, pl.segs
    op.run_hours, op.setup_hours, op.driver, op.calendar_wait_h = pl.run_h, pl.setup_h, pl.driver, pl.wait_h
    op.issues = pl.issues
    op.blocked = None


def _clear(op: Op, reason: Optional[str]) -> None:
    op.start = op.finish = None
    op.segments, op.run_hours, op.setup_hours, op.driver = [], 0.0, 0.0, ""
    if reason:
        op.blocked = reason


def _cand_for(ctx: Ctx, op: Op) -> Optional[Candidate]:
    cands, _ = ctx.candidates(op.sku, op.plant, op.process)
    return next((c for c in cands if c.resource == op.resource and (op.recipe in (None, c.recipe))), None)


# ----------------------------------------------------------------------------- generate / resolve
def generate(inp: PlanInput, ctx: Optional[Ctx] = None) -> Schedule:
    """Forward, finite-capacity, transparent heuristic (see docs/SCHEDULING.md)."""
    ctx = ctx or Ctx(inp)
    sched, reqs = build_schedule_skeleton(inp, ctx)
    w = inp.config.objective_weights
    load = defaultdict(float)
    rank = {r.order.key: r.rank for r in reqs}
    level = {}
    for op in sched.ops.values():
        level[op.id] = 0 if not op.preds else level[op.preds[0][0]] + 1
    for lv in range(0, 3):
        ops = [o for o in sched.ops.values() if level[o.id] == lv]
        if lv == 0:
            ops.sort(key=lambda o: (rank[o.order_key], o.id))
        else:
            def ready_key(o):
                comps, blocked, _ = _pred_ready(ctx, sched, o)
                return (max(comps.values()).date() if comps else date.max, rank[o.order_key], o.id)
            ops.sort(key=ready_key)
        for op in ops:
            if op.blocked:
                continue
            cands, rejected = ctx.candidates(op.sku, op.plant, op.process)
            if not cands:
                op.blocked = ("No eligible resource: " + "; ".join(sorted({r['message'] for r in rejected}))[:400]) if rejected \
                    else f"No eligible resource for {op.sku} at {op.plant} ({op.process})."
                op.candidates = rejected
                continue
            table, best = [], None
            for c in cands:
                prev_id = sched.seq.get(c.resource, [None])[-1] if sched.seq.get(c.resource) else None
                prev = sched.ops.get(prev_id) if prev_id else None
                pl = place(ctx, sched, op, c, prev)
                row = {"resource": c.resource, "recipe": c.recipe, "feasible": pl.ok}
                if not pl.ok:
                    row["reason"] = pl.reason
                    table.append(row)
                    if pl.reason.startswith(("predecessor", "CURE_", "QUALITY_")):
                        break
                    continue
                score = (w["finish"] * (pl.finish - inp.horizon_start).total_seconds() / 3600
                         + w["changeover"] * pl.setup_h + w["balance"] * load[c.resource] + w["overtime"] * pl.overtime_h)
                row.update(finish=pl.finish.isoformat(timespec="minutes"), start=pl.start.isoformat(timespec="minutes"),
                           run_hours=round(pl.run_h, 2), setup_hours=round(pl.setup_h, 2), score=round(score, 2),
                           rate=f"{c.rate.value} {c.rate.unit} ({c.rate.basis.value}{', SAP-standard fallback' if c.fallback else ''})")
                table.append(row)
                if best is None or (score, c.resource) < (best[0], best[1].resource):
                    best = (score, c, pl)
            op.candidates = table
            if best is None:
                op.blocked = next((r["reason"] for r in table if not r["feasible"]), "No feasible placement")
                continue
            _, c, pl = best
            op.resource, op.recipe = c.resource, c.recipe
            sched.seq.setdefault(c.resource, []).append(op.id)
            load[c.resource] += pl.run_h + pl.setup_h
            _commit(op, pl)
            for row in table:
                row["chosen"] = row["resource"] == c.resource and row["recipe"] == c.recipe
    resolve(inp, sched, ctx)
    return sched


def _unassigned_reason(ctx: Ctx, sched: Schedule, op: Op) -> str:
    """Re-derive WHY an operation has no assignment (never a bare 'not assigned')."""
    for pid, _ in op.preds:
        p = sched.ops[pid]
        if p.finish is None:
            return f"predecessor {p.process} ({p.id}) is not scheduled: {p.blocked or 'unscheduled'}"
    cands, rejected = ctx.candidates(op.sku, op.plant, op.process)
    if not cands:
        return "No eligible resource: " + ("; ".join(sorted({r['message'] for r in rejected}))[:400] or f"none for {op.sku} at {op.plant}")
    return "Not assigned in this plan (inputs changed after it was generated); regenerate the plan."


def resolve(inp: PlanInput, sched: Schedule, ctx: Optional[Ctx] = None) -> None:
    """Recompute every start/finish from decisions. Raises ScheduleCycle if sequences contradict precedence."""
    ctx = ctx or Ctx(inp)
    ops = sched.ops
    order_ix = {oid: i for i, oid in enumerate(ops)}
    indeg = {oid: 0 for oid in ops}
    adj = defaultdict(list)
    for o in ops.values():
        for pid, _ in o.preds:
            adj[pid].append(o.id)
            indeg[o.id] += 1
    prev_of = {}
    for r, seq in sched.seq.items():
        seq[:] = [s for s in seq if s in ops and ops[s].resource == r]
        for a, b in zip(seq, seq[1:]):
            adj[a].append(b)
            indeg[b] += 1
            prev_of[b] = a
    heap = [(order_ix[o], o) for o, d in indeg.items() if d == 0]
    heapq.heapify(heap)
    done = 0
    while heap:
        _, oid = heapq.heappop(heap)
        done += 1
        op = ops[oid]
        if op.resource is None:
            _clear(op, op.blocked or _unassigned_reason(ctx, sched, op))
        elif op.blocked and op.process == "knitting" and op.material_ready is None:
            _clear(op, None)
        else:
            cand = _cand_for(ctx, op)
            prev = ops.get(prev_of.get(oid)) if oid in prev_of else None
            if cand is None:
                _clear(op, f"Assigned resource {op.resource}/{op.recipe} is not an eligible candidate.")
            else:
                pl = place(ctx, sched, op, cand, prev)
                if pl.ok:
                    _commit(op, pl)
                else:
                    _clear(op, pl.reason)
        for n in adj[oid]:
            indeg[n] -= 1
            if indeg[n] == 0:
                heapq.heappush(heap, (order_ix[n], n))
    if done != len(ops):
        raise ScheduleCycle("Resource sequence contradicts process precedence: "
                            + ", ".join(sorted(o for o, d in indeg.items() if d > 0)[:6]))


def apply_decisions(inp: PlanInput, decisions: dict, ctx: Optional[Ctx] = None) -> Schedule:
    ctx = ctx or Ctx(inp)
    sched, _ = build_schedule_skeleton(inp, ctx)
    for oid, d in decisions.get("ops", {}).items():
        op = sched.ops.get(oid)
        if op is None:
            continue
        op.resource, op.recipe = d["resource"], d["recipe"]
        op.not_before = datetime.fromisoformat(d["not_before"]) if d.get("not_before") else None
        op.manual_by, op.manual_reason = d.get("manual_by"), d.get("manual_reason", "")
        if op.blocked and op.process == "knitting":
            op.resource = None
    sched.seq = {r: [s for s in seq if s in sched.ops and sched.ops[s].resource == r]
                 for r, seq in decisions.get("seq", {}).items()}
    resolve(inp, sched, ctx)
    return sched


# ----------------------------------------------------------------------------- validation
@dataclass
class Violation:
    rule: str
    severity: str            # hard | warning
    op: str
    message: str


def validate_schedule(inp: PlanInput, sched: Schedule, ctx: Optional[Ctx] = None) -> list[Violation]:
    """Independent re-check of hard constraints from the resolved times (used for all manual overrides)."""
    ctx = ctx or Ctx(inp)
    out: list[Violation] = []
    eps = timedelta(seconds=1)
    for op in sched.ops.values():
        if op.start is None:
            continue
        if _cand_for(ctx, op) is None:
            out.append(Violation("ELIGIBILITY", "hard", op.id, f"{op.resource}/{op.recipe} is not eligible for {op.sku} ({op.process})."))
        if op.process == "knitting" and op.material_ready and op.start + eps < op.material_ready:
            out.append(Violation("MATERIAL", "hard", op.id, "Knitting starts before material is available."))
        for pid, kind in op.preds:
            p = sched.ops[pid]
            if p.finish is None or op.start + eps < p.finish:
                out.append(Violation("PRECEDENCE", "hard", op.id, f"{op.process} starts before {p.process} output ({pid}) is complete."))
            elif kind == "cure":
                pr = ctx.route_op(p.sku, p.plant, p.process)
                rule, _ = inp.settings.resolve_cure_rule(op.plant, op.sku, p.recipe, inp.skus[op.sku].coating, inp.plan_date)
                need = p.finish + timedelta(hours=float(rule.min_cure_hours + rule.additional_processing_hours) if rule else 1e9)
                if op.start + eps < need or op.start + eps < p.finish + timedelta(hours=float(pr.qa_checkpoint_hours if pr else 0)):
                    out.append(Violation("CURE_QUALITY", "hard", op.id, "Packing starts before cure time / quality release is satisfied."))
        if op.process == "packing":
            supply = op.wip_in
            for pid, _ in op.preds:
                p = sched.ops[pid]
                pr = ctx.route_op(p.sku, p.plant, p.process)
                supply += int(Decimal(p.run_qty) * (pr.yield_pct if pr else 1)) + p.wip_in
            if op.run_qty > supply:
                out.append(Violation("PACK_QTY", "hard", op.id, f"Packing {op.run_qty} exceeds eligible dipped/WIP supply {supply}."))
    for r, seq in sched.seq.items():
        done = [sched.ops[s] for s in seq if sched.ops[s].start]
        for a, b in zip(done, done[1:]):
            if b.start + eps < a.finish:
                out.append(Violation("OVERLAP", "hard", b.id, f"Overlaps {a.id} on {r}."))
    return out


# ----------------------------------------------------------------------------- manual overrides
def apply_move(inp: PlanInput, sched: Schedule, op_id: str, resource: str, not_before: Optional[datetime],
               user: str, reason: str, recipe: Optional[str] = None):
    """Validate and apply a drag/drop style change on a COPY. Returns (new_schedule|None, violations, impact)."""
    import copy
    if not reason or not reason.strip():
        return None, [Violation("REASON_REQUIRED", "hard", op_id, "A reason is mandatory for manual overrides.")], {}
    ctx = Ctx(inp)
    if op_id not in sched.ops:
        return None, [Violation("UNKNOWN_OP", "hard", op_id, "Operation not found.")], {}
    new = copy.deepcopy(sched)
    op = new.ops[op_id]
    cands, _ = ctx.candidates(op.sku, op.plant, op.process)
    cand = next((c for c in cands if c.resource == resource and (recipe in (None, c.recipe))), None)
    if cand is None:
        return None, [Violation("ELIGIBILITY", "hard", op_id, f"{resource} is not an eligible resource for {op.sku} ({op.process}) at {op.plant}.")], {}
    if op.blocked and op.resource is None and op.process == "knitting" and op.material_ready is None:
        return None, [Violation("MATERIAL", "hard", op_id, op.blocked)], {}
    before = {o.id: (o.start, o.finish, o.resource) for o in sched.ops.values()}
    if op.resource and op_id in new.seq.get(op.resource, []):
        new.seq[op.resource].remove(op_id)
    op.resource, op.recipe, op.not_before = resource, cand.recipe, not_before
    op.manual_by, op.manual_reason = user, reason.strip()
    seq = new.seq.setdefault(resource, [])
    pos = len(seq)
    if not_before:
        for i, s in enumerate(seq):
            so = new.ops[s]
            if so.start and so.start >= not_before:
                pos = i
                break
    seq.insert(pos, op_id)
    try:
        resolve(inp, new, ctx)
    except ScheduleCycle as e:
        return None, [Violation("SEQUENCE_CYCLE", "hard", op_id, str(e))], {}
    viol = validate_schedule(inp, new, ctx)
    op = new.ops[op_id]
    if op.start is None:
        viol.append(Violation("NOT_SCHEDULABLE", "hard", op_id, op.blocked or "cannot be scheduled"))
    elif not_before and op.start > not_before + timedelta(minutes=1):
        viol.append(Violation("EARLIEST_FEASIBLE", "hard", op_id,
                              f"Requested {not_before:%Y-%m-%d %H:%M} is infeasible; earliest feasible start on {resource} is "
                              f"{op.start:%Y-%m-%d %H:%M} (limited by: {op.driver})."))
    impact = {}
    for o in new.ops.values():
        b = before.get(o.id)
        if b and (b[0], b[1], b[2]) != (o.start, o.finish, o.resource):
            impact[o.id] = {"order": o.order_key, "process": o.process,
                            "from": [x.isoformat(timespec="minutes") if x else None for x in b[:2]] + [b[2]],
                            "to": [x.isoformat(timespec="minutes") if x else None for x in (o.start, o.finish)] + [o.resource]}
    if any(v.severity == "hard" for v in viol):
        return None, viol, impact
    return new, viol, impact
