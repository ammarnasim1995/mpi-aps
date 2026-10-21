"""KPIs, capacity & utilisation, bottlenecks, material panel, exceptions, explanations, scenarios.

All numbers are produced here (deterministically) from the resolved schedule. The AI layer, if used,
may only narrate these outputs (see mpi_aps/explain_ai.py).
"""
from __future__ import annotations

import copy
import dataclasses
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from mpi_aps_core.freshness import RefreshRecord, State, Origin, assess

from .calendar import calendar_for, hours_breakdown
from .model import Downtime, Order, PlanInput
from .planner import Ctx, Schedule, apply_decisions, generate, resolve, validate_schedule

D = Decimal


def _end_of(d: date) -> datetime:
    return datetime(d.year, d.month, d.day) + timedelta(hours=23, minutes=59, seconds=59)


# ------------------------------------------------------------------------------ orders & KPIs
def order_rows(inp: PlanInput, sched: Schedule) -> list[dict]:
    by_order = defaultdict(list)
    for op in sched.ops.values():
        by_order[op.order_key].append(op)
    rows = []
    for req in sched.reqs:
        o = req.order
        ops = by_order.get(o.key, [])
        sk = inp.skus.get(o.sku)
        conv_ok = req.open_pairs is not None
        confirmed_open = None
        if conv_ok:
            c, _ = inp.converter.convert(sk.material, o.confirmed_qty, o.uom, "PR")
            dv, _ = inp.converter.convert(sk.material, o.delivered_qty, o.uom, "PR")
            confirmed_open = int(min(req.open_pairs, max(D(0), (c or D(0)) - (dv or D(0)))))
        row = {"order": o.key, "so": o.so, "line": o.line, "customer": o.customer, "sku": o.sku,
               "family": sk.family if sk else None, "plant": o.plant, "country": o.country,
               "priority": o.cust_priority, "ship_priority": o.ship_priority, "due": o.due.isoformat(),
               "ordered": str(o.qty), "uom": o.uom, "open_pairs": req.open_pairs, "open_dozens": (round(req.open_pairs / 12, 1) if conv_ok else None),
               "confirmed_open_pairs": confirmed_open,
               "unconfirmed_open_pairs": (req.open_pairs - confirmed_open) if conv_ok else None,
               "fg_used_pairs": req.fg_used, "rank": req.rank, "why_rank": req.why_rank, "origin": o.origin,
               "completion": None, "shipment_ready": None, "slack_days": None, "reason": ""}
        errs = [i for i in req.issues if i.severity == "error"]
        if errs:
            row.update(status="data_error", reason="; ".join(i.message for i in errs))
        elif req.open_pairs == 0:
            row.update(status="fulfilled", reason="Nothing open.")
        elif not ops:
            row.update(status="on_time", completion=inp.horizon_start.isoformat(timespec="minutes"),
                       shipment_ready=inp.horizon_start.isoformat(timespec="minutes"), slack_days=None,
                       reason="Open quantity fully covered by unallocated FG/WIP stock; no production needed.")
        else:
            unsched = [x for x in ops if x.finish is None]
            if unsched:
                root = next((x for x in unsched if not (x.blocked or "").startswith("predecessor")), unsched[0])
                row.update(status="unscheduled", reason=f"{root.process}: {root.blocked}")
            else:
                done = max(x.finish for x in ops)
                ready = done + timedelta(hours=inp.config.shipment_buffer_hours)
                slack = (_end_of(o.due) - ready).total_seconds() / 86400
                st = "late" if slack < 0 else ("at_risk" if slack < inp.config.at_risk_days else "on_time")
                row.update(status=st, completion=done.isoformat(timespec="minutes"),
                           shipment_ready=ready.isoformat(timespec="minutes"), slack_days=round(slack, 2))
        rows.append(row)
    return rows


def limiting_chain(sched: Schedule, order_key: str) -> list[dict]:
    """Walk back from the last finishing op, following what determined each start."""
    ops = [o for o in sched.ops.values() if o.order_key == order_key and o.finish]
    if not ops:
        return []
    cur, chain, seen = max(ops, key=lambda o: o.finish), [], set()
    while cur and cur.id not in seen:
        seen.add(cur.id)
        chain.append({"op": cur.id, "process": cur.process, "resource": cur.resource, "driver": cur.driver,
                      "start": cur.start.isoformat(timespec="minutes"), "finish": cur.finish.isoformat(timespec="minutes")})
        d = cur.driver
        if d.startswith("resource busy until "):
            nxt = sched.ops.get(d.split("until ", 1)[1])
        elif cur.preds and ("output available" in d or "cure" in d):
            nxt = sched.ops.get(cur.preds[0][0])
        else:
            break
        cur = nxt
    return chain


def kpis(inp: PlanInput, sched: Schedule, rows: Optional[list] = None) -> dict:
    rows = rows if rows is not None else order_rows(inp, sched)
    keys = {r["order"] for r in rows}
    cnt = defaultdict(int)
    for r in rows:
        cnt[r["status"]] += 1
    hours = defaultdict(float)
    ot = 0.0
    for o in sched.ops.values():
        if o.finish and o.order_key in keys:
            hours[o.process] += o.run_hours + o.setup_hours
            ot += sum(s.hours for s in o.segments if s.overtime)
    late_days = [-r["slack_days"] for r in rows if r["status"] == "late"]
    active = [r for r in rows if r["status"] not in ("fulfilled",)]
    return {
        "orders": len(active), "on_time": cnt["on_time"], "at_risk": cnt["at_risk"], "late": cnt["late"],
        "unscheduled": cnt["unscheduled"], "data_error": cnt["data_error"],
        "open_pairs": sum(r["open_pairs"] or 0 for r in rows),
        "confirmed_open_pairs": sum(r["confirmed_open_pairs"] or 0 for r in rows),
        "unconfirmed_open_pairs": sum(r["unconfirmed_open_pairs"] or 0 for r in rows),
        "scheduled_hours": {p: round(h, 1) for p, h in hours.items()},
        "total_scheduled_hours": round(sum(hours.values()), 1),
        "overtime_hours_used": round(ot, 1), "overtime_shift_equivalents": round(ot / 8, 1),
        "max_delay_days": round(max(late_days), 1) if late_days else 0,
        "unscheduled_pairs": sum(r["open_pairs"] or 0 for r in rows if r["status"] == "unscheduled"),
    }


# ------------------------------------------------------------------------------ capacity
def _buckets(inp: PlanInput, sched: Schedule, period: str) -> list[tuple[str, datetime, datetime]]:
    last = max([o.finish for o in sched.ops.values() if o.finish] + [inp.horizon_end])
    out = []
    if period == "month":
        cur = datetime(inp.plan_date.year, inp.plan_date.month, 1)
        while cur < last:
            nxt = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            out.append((cur.strftime("%Y-%m"), max(cur, inp.horizon_start), nxt))
            cur = nxt
    else:
        cur = inp.horizon_start - timedelta(days=inp.horizon_start.weekday())
        while cur < last:
            out.append((cur.strftime("%G-W%V"), max(cur, inp.horizon_start), cur + timedelta(days=7)))
            cur += timedelta(days=7)
    return out


def capacity(inp: PlanInput, sched: Schedule, period: str = "week", plant: str = None, process: str = None) -> dict:
    """Per resource & bucket: gross, planned/unplanned downtime, net, overtime available, scheduled, utilisation, remaining."""
    buckets = _buckets(inp, sched, period)
    rows = []
    for res in inp.resources.values():
        if (plant and res.plant != plant) or (process and res.process != process) or res.plant not in inp.plants:
            continue
        spec = calendar_for(res, inp.plants)
        segs = [(s, o) for o in sched.ops.values() if o.resource == res.id for s in o.segments]
        for label, b0, b1 in buckets:
            hb = hours_breakdown(spec, res.downtimes, b0, b1)
            sch = ot_used = 0.0
            for s, o in segs:
                ov = (min(s.end, b1) - max(s.start, b0)).total_seconds() / 3600
                if ov > 0:
                    sch += ov
                    ot_used += ov if s.overtime else 0
            avail = hb["net"] + hb["overtime_available"]
            rows.append({"resource": res.id, "plant": res.plant, "process": res.process, "bucket": label,
                         **{k: round(v, 2) for k, v in hb.items()}, "available": round(avail, 2),
                         "scheduled": round(sch, 2), "overtime_used": round(ot_used, 2),
                         "utilization_pct": round(100 * sch / avail, 1) if avail > 0 else (None if sch == 0 else 999.0),
                         "remaining": round(max(0.0, avail - sch), 2)})
    return {"period": period, "definition": "utilization % = scheduled production hours (run+setup) / (net available + approved overtime) hours x 100",
            "buckets": [b[0] for b in buckets], "rows": rows}


def rough_cut(inp: PlanInput, sched: Schedule, ctx: Optional[Ctx] = None) -> dict:
    """Infinite-capacity requirement per resource (fastest eligible candidate), cumulated by order due week,
    versus cumulative net capacity. Optimistic: ignores downstream lead times. Flags overloaded resources."""
    ctx = ctx or Ctx(inp)
    weeks = _buckets(inp, sched, "week")
    demand = defaultdict(lambda: defaultdict(float))
    unmapped = []
    for op in sched.ops.values():
        if op.blocked and not op.blocked.startswith("predecessor") and not op.candidates:
            pass
        cands, _ = ctx.candidates(op.sku, op.plant, op.process)
        best = None
        for c in cands:
            hr = ctx.hours(op, c, D(0))
            if hr.ok and (best is None or hr.run_hours < best[1]):
                best = (c, hr.run_hours)
        if best is None:
            unmapped.append(op.id)
            continue
        due = next(o.due for o in inp.orders if o.key == op.order_key)
        wk = next((w[0] for w in weeks if w[1] <= _end_of(due) < w[2]), weeks[-1][0])
        demand[best[0].resource][wk] += float(best[1])
    out = []
    for rid, per in demand.items():
        res = inp.resources[rid]
        if res.plant not in inp.plants:
            continue
        spec = calendar_for(res, inp.plants)
        cum_d = cum_c = 0.0
        worst = 0.0
        series = []
        for label, b0, b1 in weeks:
            hb = hours_breakdown(spec, res.downtimes, b0, b1)
            cap = hb["net"] + hb["overtime_available"]
            cum_c += cap
            cum_d += per.get(label, 0.0)
            worst = max(worst, cum_d - cum_c)
            series.append({"bucket": label, "demand": round(per.get(label, 0.0), 1), "capacity": round(cap, 1),
                           "cum_demand": round(cum_d, 1), "cum_capacity": round(cum_c, 1)})
        out.append({"resource": rid, "plant": res.plant, "process": res.process, "overloaded": worst > 0.5,
                    "max_cumulative_shortage_h": round(worst, 1), "total_demand_h": round(cum_d, 1), "series": series})
    out.sort(key=lambda r: -r["max_cumulative_shortage_h"])
    return {"method": "rough-cut, infinite capacity, fastest eligible resource, due-week bucketing (optimistic)",
            "resources": out, "unmapped_ops": unmapped}


def bottlenecks(inp: PlanInput, sched: Schedule, rows: Optional[list] = None) -> list[dict]:
    rows = rows if rows is not None else order_rows(inp, sched)
    late = {r["order"] for r in rows if r["status"] in ("late", "at_risk")}
    limited = defaultdict(set)
    for ok in late:
        for step in limiting_chain(sched, ok):
            if step["driver"].startswith("resource busy"):
                limited[step["resource"]].add(ok)
    res_rows = []
    for res in inp.resources.values():
        if res.plant not in inp.plants:
            continue
        ops = [o for o in sched.ops.values() if o.resource == res.id and o.finish]
        if not ops:
            continue
        last = max(o.finish for o in ops)
        hb = hours_breakdown(calendar_for(res, inp.plants), res.downtimes, inp.horizon_start, last)
        sch = sum(o.run_hours + o.setup_hours for o in ops)
        avail = hb["net"] + hb["overtime_available"]
        res_rows.append({"resource": res.id, "plant": res.plant, "process": res.process,
                         "busy_until": last.isoformat(timespec="minutes"), "scheduled_h": round(sch, 1),
                         "available_h_to_busy_until": round(avail, 1),
                         "utilization_pct": round(100 * sch / avail, 1) if avail else None,
                         "late_or_at_risk_orders_limited": len(limited.get(res.id, ())),
                         "orders": sorted(limited.get(res.id, ()))})
    res_rows.sort(key=lambda r: (-r["late_or_at_risk_orders_limited"], -(r["utilization_pct"] or 0)))
    return res_rows


# ------------------------------------------------------------------------------ materials / WIP
def material_panel(inp: PlanInput, sched: Schedule) -> dict:
    need = defaultdict(Decimal)
    blocked, delayed = [], []
    for op in sched.ops.values():
        if op.process != "knitting":
            continue
        for b in (b for b in inp.boms if b.sku == op.sku):
            need[(b.component, op.plant)] += Decimal(op.run_qty) * b.qty_per_pair
        if op.blocked and not op.blocked.startswith("predecessor"):
            blocked.append({"op": op.id, "order": op.order_key, "reason": op.blocked})
        elif op.material_ready and op.material_ready > inp.horizon_start:
            delayed.append({"op": op.id, "order": op.order_key, "material_ready": op.material_ready.isoformat(timespec="minutes"),
                            "note": op.material_note})
    rec = defaultdict(list)
    for r in inp.receipts:
        rec[(r.material, r.plant)].append({"on": r.on.isoformat(), "qty": str(r.qty)})
    rows = []
    for s in inp.raw_stocks:
        k = (s.material, s.plant)
        rule = inp.material_rules.get(s.material)
        rows.append({"material": s.material, "plant": s.plant, "unit": s.unit, "stock": str(s.qty), "reserved": str(s.reserved),
                     "quality_blocked": str(s.blocked), "available": str(s.available),
                     "required_by_plan": str(round(need[k], 1)), "shortage": str(round(max(Decimal(0), need[k] - s.available), 1)),
                     "expected_receipts": rec[k], "on_shortage_rule": rule.on_shortage if rule else "delay (default)", "origin": s.origin})
    wip = [{"sku": s.sku, "plant": s.plant, "stage": s.stage, "qty": str(s.qty), "allocated": str(s.allocated),
            "quality_blocked": str(s.blocked), "available": str(s.available), "origin": s.origin} for s in inp.stocks]
    return {"raw_materials": rows, "wip_fg": wip, "blocked_ops": blocked, "delayed_ops": delayed}


# ------------------------------------------------------------------------------ freshness
def freshness(inp: PlanInput, now: datetime, extra_history: Optional[list] = None) -> list[dict]:
    hist = [RefreshRecord(r.source, Origin(r.origin), r.attempted_at, r.success, r.file_name, tuple(r.errors))
            for r in inp.refresh_log] + (extra_history or [])
    out = []
    for src in sorted({h.source for h in hist}):
        a = assess(src, hist, now, inp.settings)
        out.append({"source": a.source, "state": a.state.value, "label": a.label,
                    "age_hours": round(float(a.age_hours), 1) if a.age_hours is not None else None,
                    "last_attempt": a.last_attempt_at.isoformat(timespec="minutes") if a.last_attempt_at else None,
                    "last_success": a.last_success_at.isoformat(timespec="minutes") if a.last_success_at else None,
                    "last_attempt_failed": a.last_attempt_failed, "errors": list(a.errors),
                    "critical_warning": a.critical_warning, "affected_decisions": list(a.affected_decisions),
                    "acknowledgment_permitted": a.acknowledgment_permitted})
    return out


# ------------------------------------------------------------------------------ exceptions
def exceptions(inp: PlanInput, sched: Schedule, findings: list, fresh: list, rows=None, rc=None) -> list[dict]:
    rows = rows if rows is not None else order_rows(inp, sched)
    ex = []
    for r in rows:
        if r["status"] == "late":
            ex.append({"severity": "high", "category": "delivery", "title": f"{r['order']} projected late by {-r['slack_days']:.1f} days",
                       "detail": f"Completion {r['completion']} vs due {r['due']}.", "records": [r["order"]],
                       "action": "Open the explanation for alternatives (expedite, overtime, alternative resource)."})
        elif r["status"] == "at_risk":
            ex.append({"severity": "medium", "category": "delivery", "title": f"{r['order']} at risk ({r['slack_days']:.1f} days slack)",
                       "detail": f"Completion {r['completion']} vs due {r['due']}.", "records": [r["order"]], "action": "Review resource load and priority."})
        elif r["status"] == "unscheduled":
            ex.append({"severity": "high", "category": "scheduling", "title": f"{r['order']} cannot be scheduled",
                       "detail": r["reason"], "records": [r["order"]], "action": "Resolve the stated data/material/constraint issue."})
        elif r["status"] == "data_error":
            ex.append({"severity": "high", "category": "data", "title": f"{r['order']} has a data error", "detail": r["reason"],
                       "records": [r["order"]], "action": "Correct master/order data; nothing is auto-repaired."})
    for res in (rc or rough_cut(inp, sched))["resources"]:
        if res["overloaded"]:
            ex.append({"severity": "high", "category": "capacity", "title": f"{res['resource']} overloaded (rough-cut)",
                       "detail": f"Cumulative demand exceeds net capacity by {res['max_cumulative_shortage_h']} h.",
                       "records": [res["resource"]], "action": "Reallocate to eligible resources, add overtime, or move orders."})
    for f in findings:
        if f["severity"] == "error":
            ex.append({"severity": "medium", "category": "data_quality", "title": f"{f['code']}: {f['message']}"[:160],
                       "detail": f["message"], "records": f["records"][:10], "action": f["action"]})
    for f in fresh:
        if f["critical_warning"] or f["state"] == "never_refreshed":
            ex.append({"severity": "high", "category": "data_freshness", "title": f"Stale/failed critical source: {f['source']}",
                       "detail": f["label"], "records": [f["source"]],
                       "action": "Refresh the source; affected decisions: " + ", ".join(f["affected_decisions"] or ["n/a"])})
    mp = material_panel(inp, sched)
    for b in mp["blocked_ops"]:
        ex.append({"severity": "high", "category": "material", "title": f"Material prevents {b['order']}", "detail": b["reason"],
                   "records": [b["order"]], "action": "Procure/receive material or change the shortage rule (authorised users)."})
    for b in mp["delayed_ops"]:
        ex.append({"severity": "medium", "category": "material", "title": f"Material delays {b['order']} to {b['material_ready'][:10]}",
                   "detail": b["note"], "records": [b["order"]], "action": "Expedite receipt or accept delay."})
    sev = {"high": 0, "medium": 1, "low": 2}
    ex.sort(key=lambda e: sev[e["severity"]])
    for i, e in enumerate(ex, 1):
        e["id"] = f"EX-{i:04d}"
    return ex


# ------------------------------------------------------------------------------ explanations
def explain_order(inp: PlanInput, sched: Schedule, order_key: str, with_alternatives: bool = True) -> dict:
    rows = {r["order"]: r for r in order_rows(inp, sched)}
    r = rows.get(order_key)
    if r is None:
        return {"error": "Order not found or not open."}
    ops = [o for o in sched.ops.values() if o.order_key == order_key]
    lines = [f"Priority: rank {r['rank']} - {r['why_rank']}."]
    for o in ops:
        if o.finish:
            sel = next((c for c in o.candidates if c.get("chosen")), None)
            alt = [c for c in o.candidates if c.get("feasible") and not c.get("chosen")]
            why = (f"{o.resource} chosen: finish {sel['finish']} score {sel['score']} (lowest weighted score of finish time, "
                   f"changeover, load balance, overtime)." if sel else f"{o.resource} set by planner ({o.manual_by}): {o.manual_reason}.")
            lines.append(f"{o.process} {o.id}: {o.run_qty} pairs on {o.resource}, {o.run_hours:.1f} h run + {o.setup_hours:.1f} h setup, "
                         f"{o.start:%Y-%m-%d %H:%M} to {o.finish:%Y-%m-%d %H:%M}. Start limited by: {o.driver}. {why}"
                         + (f" Alternatives: " + "; ".join(f"{a['resource']} finish {a['finish']}" for a in alt) if alt else ""))
        else:
            lines.append(f"{o.process} {o.id}: NOT SCHEDULED - {o.blocked}")
    chain = limiting_chain(sched, order_key)
    out = {"order": r, "narrative": lines, "limiting_chain": chain, "ops": [
        {"op": o.id, "process": o.process, "resource": o.resource, "recipe": o.recipe, "run_qty": o.run_qty,
         "start": o.start.isoformat(timespec="minutes") if o.start else None,
         "finish": o.finish.isoformat(timespec="minutes") if o.finish else None, "driver": o.driver,
         "blocked": o.blocked, "candidates": o.candidates, "material_note": o.material_note} for o in ops]}
    if with_alternatives and r["status"] in ("late", "at_risk"):
        alts = []
        base = {x["order"]: x for x in order_rows(inp, sched)}
        bind = next((s for s in chain if s["driver"].startswith("resource busy")), None) or (chain[0] if chain else None)
        tests = [("Expedite this order to top priority", [{"type": "expedite", "orders": [order_key]}])]
        if bind and bind.get("resource"):
            tests.append((f"Add 2 h/day overtime on {bind['resource']} for 4 weeks",
                          [{"type": "overtime", "resource": bind["resource"], "hours_per_day": 2, "days": 28}]))
        for label, ch in tests:
            res = run_scenario(inp, ch, mode="replan")
            nr = {x["order"]: x for x in res["orders"]}
            alts.append({"action": label, "completion": nr[order_key]["completion"], "status": nr[order_key]["status"],
                         "slack_days": nr[order_key]["slack_days"],
                         "orders_worsened": sum(1 for k, x in nr.items() if k != order_key and base[k]["status"] != x["status"]
                                                and ["on_time", "at_risk", "late", "unscheduled"].index(x["status"]) > ["on_time", "at_risk", "late", "unscheduled"].index(base[k]["status"])
                                                if base[k]["status"] in ("on_time", "at_risk", "late", "unscheduled") and x["status"] in ("on_time", "at_risk", "late", "unscheduled")),
                         "kpis": res["kpis"]})
        out["alternatives"] = alts
        out["alternatives_note"] = "Each alternative is a full engine re-plan of a copy; the approved plan is untouched."
    return out


# ------------------------------------------------------------------------------ scenarios
def apply_changes(inp: PlanInput, changes: list[dict]) -> list[str]:
    """Mutates `inp` (a deep copy). Returns human-readable notes. Unknown change types raise ValueError."""
    notes = []
    for ch in changes:
        t = ch.get("type")
        if t == "overtime":
            targets = [r for r in inp.resources.values() if
                       (ch.get("resource") and r.id == ch["resource"]) or
                       (not ch.get("resource") and (not ch.get("plant") or r.plant == ch["plant"])
                        and (not ch.get("process") or r.process == ch["process"]))]
            if not targets:
                raise ValueError("overtime: no matching resources")
            h = float(ch.get("hours_per_day", 2))
            start = date.fromisoformat(ch["from"]) if ch.get("from") else inp.plan_date
            for r in targets:
                spec = copy.deepcopy(calendar_for(r, inp.plants))
                end_h = max(b for _, b in spec.shifts)
                for i in range(int(ch.get("days", 28))):
                    d = start + timedelta(days=i)
                    if d.weekday() in spec.working_weekdays and d not in spec.holidays and end_h + h <= 24:
                        spec.overtime.setdefault(d, []).append((end_h, end_h + h))
                r.calendar = spec
            notes.append(f"Overtime {h} h/day added on {len(targets)} resource(s).")
        elif t in ("breakdown", "maintenance"):
            r = inp.resources[ch["resource"]]
            s = datetime.fromisoformat(ch["start"]) if ch.get("start") else inp.horizon_start + timedelta(days=float(ch.get("start_day", 0)))
            e = datetime.fromisoformat(ch["end"]) if ch.get("end") else s + timedelta(hours=float(ch.get("hours", 24)))
            r.downtimes.append(Downtime(s, e, "breakdown" if t == "breakdown" else "maintenance", ch.get("reason", "scenario")))
            notes.append(f"{t} on {r.id} {s:%Y-%m-%d %H:%M} to {e:%Y-%m-%d %H:%M}.")
        elif t == "rate_change":
            n = 0
            for i, rt in enumerate(inp.rates):
                if ((not ch.get("resource") or rt.resource == ch["resource"]) and (not ch.get("plant") or rt.plant == ch["plant"])
                        and (not ch.get("process") or rt.process == ch["process"]) and (not ch.get("sku") or rt.sku == ch["sku"])
                        and rt.kind.value == inp.config.rate_kind):
                    inp.rates[i] = dataclasses.replace(rt, value=rt.value * D(str(ch["factor"])), source=rt.source + " [SCENARIO-HYPOTHETICAL]")
                    n += 1
            if not n:
                raise ValueError("rate_change: no matching rates")
            notes.append(f"{n} rate(s) multiplied by {ch['factor']} (hypothetical).")
        elif t == "demand":
            n = 0
            for o in inp.orders:
                if ((not ch.get("order") or o.key == ch["order"]) and (not ch.get("sku") or o.sku == ch["sku"])
                        and (not ch.get("customer") or o.customer == ch["customer"])):
                    o.qty = o.qty * (D(1) + D(str(ch["pct"])) / D(100))
                    n += 1
            notes.append(f"Demand changed {ch['pct']}% on {n} order(s).")
        elif t == "material_delay":
            n = 0
            for rc in inp.receipts:
                if rc.material == ch["material"] and (not ch.get("plant") or rc.plant == ch["plant"]):
                    rc.on = rc.on + timedelta(days=int(ch["days"]))
                    n += 1
            notes.append(f"{n} receipt(s) of {ch['material']} delayed {ch['days']} days.")
        elif t == "move_plant":
            o = next(x for x in inp.orders if x.key == ch["order"])
            o.plant, o.country = ch["plant"], inp.plants[ch["plant"]].country
            notes.append(f"Order {o.key} moved to {ch['plant']}.")
        elif t == "expedite":
            for i, k in enumerate(ch["orders"]):
                inp.priority_overrides[k] = (-(len(ch["orders"]) - i), "scenario: expedite", "scenario")
            notes.append(f"Expedited {', '.join(ch['orders'])}.")
        elif t == "lots":
            inp.config.lots_by_order[ch["order"]] = int(ch["lots"])
            notes.append(f"Order {ch['order']} split into {ch['lots']} lots.")
        else:
            raise ValueError(f"Unknown scenario change type: {t!r}")
    return notes


def run_scenario(inp: PlanInput, changes: list[dict], mode: str = "replan", baseline_decisions: Optional[dict] = None) -> dict:
    """Simulate on a deep copy; never touches `inp` or any stored plan.
    mode 'replan' = engine re-plans from scratch; 'resolve' = keep baseline decisions, recompute times (impact if nothing is done)."""
    work = copy.deepcopy(inp)
    notes = apply_changes(work, changes)
    if mode == "resolve" and baseline_decisions:
        sched = apply_decisions(work, baseline_decisions)
    else:
        sched = generate(work)
    rows = order_rows(work, sched)
    return {"mode": mode, "notes": notes, "kpis": kpis(work, sched, rows), "orders": rows,
            "violations": [dataclasses.asdict(v) for v in validate_schedule(work, sched)],
            "decisions": sched.decisions(), "schedule": sched, "input": work}


def compare(base: dict, scen: dict) -> dict:
    b = {r["order"]: r for r in base["orders"]}
    rows = []
    for r in scen["orders"]:
        x = b.get(r["order"])
        if not x:
            continue
        delta = None
        if x["completion"] and r["completion"]:
            delta = round((datetime.fromisoformat(r["completion"]) - datetime.fromisoformat(x["completion"])).total_seconds() / 86400, 2)
        if delta or x["status"] != r["status"]:
            rows.append({"order": r["order"], "customer": r["customer"], "sku": r["sku"], "base_status": x["status"], "scenario_status": r["status"],
                         "base_completion": x["completion"], "scenario_completion": r["completion"], "delta_days": delta})
    bk, sk = base["kpis"], scen["kpis"]
    keys = ["on_time", "at_risk", "late", "unscheduled", "total_scheduled_hours", "overtime_hours_used",
            "overtime_shift_equivalents", "max_delay_days", "unscheduled_pairs"]
    return {"notes": scen["notes"], "kpi_delta": {k: {"baseline": bk[k], "scenario": sk[k], "delta": round(sk[k] - bk[k], 2)} for k in keys},
            "hours_by_process": {p: {"baseline": bk["scheduled_hours"].get(p, 0), "scenario": sk["scheduled_hours"].get(p, 0)}
                                 for p in ("knitting", "dipping", "packing")},
            "orders_changed": sorted(rows, key=lambda r: -(abs(r["delta_days"] or 0)))}


def breakdown_impact(inp: PlanInput, baseline_decisions: dict, resource: str, start: str, end: str) -> dict:
    """Schedule-impact analysis of a breakdown: (a) keep the plan, (b) replan. Lists affected orders."""
    ch = [{"type": "breakdown", "resource": resource, "start": start, "end": end}]
    base_sched = apply_decisions(inp, baseline_decisions)
    base = {"kpis": kpis(inp, base_sched), "orders": order_rows(inp, base_sched)}
    keep = run_scenario(inp, ch, "resolve", baseline_decisions)
    replan = run_scenario(inp, ch, "replan")
    hit_ops = sorted({o.order_key for o in keep["schedule"].ops.values() if o.resource == resource and o.start and
                      o.start < datetime.fromisoformat(end) and o.finish > datetime.fromisoformat(start)})
    return {"resource": resource, "window": [start, end], "orders_on_resource_during_outage": hit_ops,
            "if_plan_unchanged": compare(base, keep), "after_replan": compare(base, replan)}
