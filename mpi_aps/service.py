"""Application service: orchestrates engine, store, audit and permissions. Holds no business formulas."""
from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from mpi_aps_core.freshness import Origin
from mpi_aps_core.settings import Approval, CureRule, EfficiencyFactor, FreshnessPolicy, Scope

from . import analytics as an
from . import importer
from .auth import Principal
from .model import Order, RefreshInfo
from .planner import Ctx, ScheduleCycle, apply_decisions, apply_move, generate, validate_schedule
from .sample import build_sample
from .store import ConflictError, PlanLocked, Store
from . import validation as val

log = logging.getLogger("mpi_aps")


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


def order_from_payload(p: dict) -> Order:
    return Order(p["so"], p["line"], p["customer"], p["sku"], Decimal(p["qty"]), p["uom"], Decimal(p["delivered_qty"]),
                 Decimal(p["confirmed_qty"]), date.fromisoformat(p["req_date"]),
                 date.fromisoformat(p["conf_date"]) if p.get("conf_date") not in (None, "None") else None,
                 int(p["cust_priority"]), int(p["ship_priority"]), p["country"], p["plant"], p["status"], p["origin"])


def _ser(o):
    d = dataclasses.asdict(o)
    for k, v in d.items():
        if isinstance(v, (date, datetime)):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = str(v)
        elif isinstance(v, Scope) or isinstance(v, Approval):
            d[k] = v.value
    return d


class AppService:
    def __init__(self, store: Store, plan_date: date, four_eyes: bool = True, now_fn=None):
        self.store, self.plan_date, self.four_eyes = store, plan_date, four_eyes
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.version = 0
        self._cache: dict = {}
        self.rebuild()
        if not self.store.list_plans("plan"):
            self.generate_plan(Principal("system", "central_planning_admin", ("*",)), "Initial draft (sample data)")

    # ------------------------------------------------------------------ inputs
    def rebuild(self) -> None:
        with self.lock:
            inp = build_sample(self.plan_date, now=self.now())
            for op in self.store.kv_get("settings_ops", []):
                self._apply_setting(inp, op, replay=True)
            if self.store.kv_get("sample_orders_removed", False):
                inp.orders = [o for o in inp.orders if o.origin != "sample"]
            have = {o.key for o in inp.orders}
            inp.orders += [o for o in map(order_from_payload, self.store.stored_orders()) if o.key not in have]
            inp.priority_overrides = self.store.overrides()
            for r in reversed(self.store.refresh_list(500)):
                inp.refresh_log.append(RefreshInfo(r["source"], r["origin"], datetime.fromisoformat(r["attempted_at"]),
                                                   bool(r["success"]), r["file_name"], tuple(r["errors"]), r["rows"]))
            self.inp = inp
            self.version += 1
            self._cache.clear()

    def _apply_setting(self, inp, op, replay=False):
        k, p = op["kind"], op["payload"]
        st = inp.settings
        user, reason = op["user"], op["reason"]
        d = lambda s: date.fromisoformat(s) if s else None
        if k == "efficiency":
            f = EfficiencyFactor(Scope(p["scope"]), Decimal(p["factor"]), d(p["effective_from"]), p["source"], Approval(p["approval_status"]),
                                 p.get("plant"), p.get("process"), p.get("resource"), d(p.get("effective_to")))
            st.put_efficiency_factor(user, f, reason)
        elif k == "cure":
            r = CureRule(Decimal(p["min_cure_hours"]), d(p["effective_from"]), p["source"], Approval(p["approval_status"]), p.get("plant"),
                         p.get("sku"), p.get("recipe"), p.get("coating"), Decimal(p.get("additional_processing_hours", "0")), d(p.get("effective_to")))
            st.put_cure_rule(user, r, reason)
        elif k == "freshness":
            st.put_freshness_policy(user, FreshnessPolicy(p["source"], Decimal(p["threshold_hours"]), p.get("process"), bool(p.get("critical")),
                                                         tuple(p.get("affected_decisions", [])), bool(p.get("acknowledgment_permitted"))), reason)
        else:
            raise BadRequest(f"unknown setting kind {k}")

    def put_setting(self, pr: Principal, kind: str, payload: dict, reason: str):
        self._need(pr, "settings")
        if not reason or not reason.strip():
            raise BadRequest("A reason is required for settings changes.")
        op = {"kind": kind, "payload": payload, "user": pr.user, "reason": reason}
        with self.lock:
            try:
                before = self.inp.settings.version
                self._apply_setting(self.inp, op)
            except (ValueError, KeyError, TypeError) as e:
                raise BadRequest(str(e))
            ops = self.store.kv_get("settings_ops", [])
            ops.append(op)
            self.store.kv_set("settings_ops", ops)
            self.store.audit(pr.user, f"settings.{kind}", str(payload.get("source") or payload.get("coating") or kind), None, payload, reason)
            affected = self.inp.settings.affected_scopes_since(before)
            self.version += 1
            self._cache.clear()
            return {"saved": True, "affected_scopes": affected,
                    "note": "Existing drafts are re-resolved with the new settings; approved/published plans are not changed."}

    # ------------------------------------------------------------------ helpers
    def _need(self, pr: Principal, perm: str) -> None:
        if not pr.can(perm):
            raise Forbidden(f"Role '{pr.role}' lacks permission '{perm}'.")

    def _plants(self, pr: Principal):
        return None if (pr.can("view_all_plants") or "*" in pr.plants) else set(pr.plants)

    def _working_plan(self, plan_id=None):
        if plan_id is None:
            drafts = [p for p in self.store.list_plans("plan") if p["status"] == "draft"]
            if drafts:
                plan_id = drafts[0]["id"]
            else:
                b = self.store.baseline_plan()
                plan_id = b["id"] if b else None
        if plan_id is None:
            raise NotFound("No plan exists. Generate one first.")
        plan = self.store.get_plan(int(plan_id))
        if not plan:
            raise NotFound(f"Plan {plan_id} not found.")
        return plan

    def schedule_for(self, plan: dict):
        key = (plan["id"], self.version)
        if key not in self._cache:
            self._cache[key] = apply_decisions(self.inp, plan["decisions"])
        return self._cache[key]

    def _snapshot(self, sched) -> dict:
        rows = an.order_rows(self.inp, sched)
        return {"kpis": an.kpis(self.inp, sched, rows), "orders": rows, "ops": self.gantt_ops(sched),
                "plan_date": self.plan_date.isoformat(), "inputs_version": self.version,
                "data_origins": self.origins()}

    @staticmethod
    def gantt_ops(sched) -> list:
        return [{"id": o.id, "order": o.order_key, "lot": o.lot_id, "process": o.process, "resource": o.resource, "plant": o.plant,
                 "sku": o.sku, "qty": o.run_qty, "start": o.start.isoformat(timespec="minutes") if o.start else None,
                 "finish": o.finish.isoformat(timespec="minutes") if o.finish else None, "run_h": round(o.run_hours, 2),
                 "setup_h": round(o.setup_hours, 2), "driver": o.driver, "blocked": o.blocked,
                 "manual": bool(o.manual_by), "manual_by": o.manual_by, "manual_reason": o.manual_reason,
                 "overtime_h": round(sum(s.hours for s in o.segments if s.overtime), 2)} for o in sched.ops.values()]

    def origins(self) -> dict:
        c = {}
        for coll in (self.inp.orders, self.inp.stocks, self.inp.raw_stocks, self.inp.resources.values()):
            for x in coll:
                c[x.origin] = c.get(x.origin, 0) + 1
        for r in self.inp.rates:                      # core rate records carry `source`, not `origin`
            o = "sample" if r.source.startswith("SAMPLE") else "upload"
            c[o] = c.get(o, 0) + 1
        return c

    @staticmethod
    def _match(row, f, allowed):
        if allowed is not None and row["plant"] not in allowed:
            return False
        for k, rk in (("plant", "plant"), ("country", "country"), ("family", "family"), ("customer", "customer"),
                      ("sku", "sku"), ("order", "order"), ("status", "status")):
            if f.get(k) and str(row.get(rk)) != f[k]:
                return False
        if f.get("priority") and str(row["priority"]) != f["priority"]:
            return False
        if f.get("date_from") and row["due"] < f["date_from"]:
            return False
        if f.get("date_to") and row["due"] > f["date_to"]:
            return False
        return True

    def filtered_rows(self, sched, f, pr):
        allowed = self._plants(pr)
        rows = an.order_rows(self.inp, sched)
        rows = [r for r in rows if self._match(r, f, allowed)]
        if f.get("process") or f.get("resource"):
            keep = {o.order_key for o in sched.ops.values() if (not f.get("process") or o.process == f["process"])
                    and (not f.get("resource") or o.resource == f["resource"])}
            rows = [r for r in rows if r["order"] in keep]
        return rows

    # ------------------------------------------------------------------ read APIs
    def meta(self, pr: Principal):
        plans = self.store.list_plans("plan")
        fresh = an.freshness(self.inp, self.now())
        return {"user": pr.user, "role": pr.role, "plants_scope": list(pr.plants), "permissions": sorted(
            p for p in ("view", "edit_schedule", "master_data", "import", "run_scenarios", "approve", "publish", "settings", "ack_stale", "view_audit") if pr.can(p)),
            "plan_date": self.plan_date.isoformat(), "data_origins": self.origins(),
            "data_banner": ("SAMPLE DATA - not live SAP data" if set(self.origins()) <= {"sample"} else
                            "Mixed data: " + ", ".join(f"{k}={v}" for k, v in self.origins().items())),
            "plants": [{"id": p.id, "country": p.country, "name": p.name} for p in self.inp.plants.values()],
            "countries": sorted({p.country for p in self.inp.plants.values()}),
            "families": sorted({s.family for s in self.inp.skus.values()}), "skus": sorted(self.inp.skus),
            "customers": sorted({o.customer for o in self.inp.orders}),
            "resources": [{"id": r.id, "plant": r.plant, "process": r.process, "group": r.group} for r in self.inp.resources.values()],
            "plans": plans, "freshness": fresh, "schema_version": self.store.schema_version(),
            "ai": {"enabled": False, "note": "Explanations are rule-generated by the planning engine. No LLM is connected; AI may only narrate engine output."},
            "inputs_version": self.version}

    def dashboard(self, pr, f, plan_id=None):
        plan = self._working_plan(plan_id)
        sched = self.schedule_for(plan)
        rows = self.filtered_rows(sched, f, pr)
        k = an.kpis(self.inp, sched, rows)
        allowed = self._plants(pr)
        cap = an.capacity(self.inp, sched, "week", f.get("plant"), f.get("process"))
        util = {}
        window = set(cap["buckets"][:4])          # utilisation KPI = next 4 weeks
        for r in cap["rows"]:
            if (allowed is not None and r["plant"] not in allowed) or r["bucket"] not in window:
                continue
            u = util.setdefault(r["process"], [0.0, 0.0])
            u[0] += r["scheduled"]
            u[1] += r["available"]
        mat = an.material_panel(self.inp, sched)
        stocks = [s for s in self.inp.stocks if allowed is None or s.plant in allowed]
        wip = {st: int(sum(s.available for s in stocks if s.stage == st)) for st in ("KNIT_WIP", "DIP_WIP", "FG")}
        late = [r for r in rows if r["status"] == "late"]
        bn = [b for b in an.bottlenecks(self.inp, sched, rows) if allowed is None or b["plant"] in allowed][:5]
        fresh = an.freshness(self.inp, self.now())
        return {"plan": {k2: plan[k2] for k2 in ("id", "name", "status", "created_by", "created_at")},
                "plan_stale": plan["status"] == "draft" and plan["snapshot"].get("inputs_version") != self.version,
                "kpis": k, "dozens": {"open": round(k["open_pairs"] / 12, 1), "confirmed": round(k["confirmed_open_pairs"] / 12, 1),
                                      "unconfirmed": round(k["unconfirmed_open_pairs"] / 12, 1)},
                "utilization_by_process": {p: round(100 * a / b, 1) if b else None for p, (a, b) in util.items()},
                "material": {"blocked_ops": len(mat["blocked_ops"]), "delayed_ops": len(mat["delayed_ops"])},
                "wip_fg_pairs": wip,
                "projected_delays": [{"order": r["order"], "customer": r["customer"], "days_late": -r["slack_days"]} for r in sorted(late, key=lambda r: r["slack_days"])[:8]],
                "bottlenecks": bn,
                "plan_adherence": {"available": False, "reason": "No production-confirmation (actuals) source is connected yet."},
                "freshness": fresh, "data_banner": self.meta(pr)["data_banner"],
                "drill": {"kpi_orders": {s: [r["order"] for r in rows if r["status"] == s] for s in ("on_time", "at_risk", "late", "unscheduled", "data_error")}}}

    def orders(self, pr, f, plan_id=None):
        sched = self.schedule_for(self._working_plan(plan_id))
        rows = self.filtered_rows(sched, f, pr)
        cons = {}
        for r in rows:
            if r["status"] in ("fulfilled",):
                continue
            c = cons.setdefault((r["sku"], r["plant"]), {"sku": r["sku"], "plant": r["plant"], "orders": 0, "open_pairs": 0, "unallocated_pairs": 0})
            c["orders"] += 1
            c["open_pairs"] += r["open_pairs"] or 0
            if r["status"] in ("unscheduled", "data_error"):
                c["unallocated_pairs"] += r["open_pairs"] or 0
        return {"orders": rows, "consolidated_by_sku": sorted(cons.values(), key=lambda c: -c["open_pairs"]),
                "overrides": {k: {"rank": v[0], "reason": v[1], "user": v[2]} for k, v in self.inp.priority_overrides.items()}}

    def planning(self, pr, process, f, plan_id=None):
        plan = self._working_plan(plan_id)
        sched = self.schedule_for(plan)
        allowed = self._plants(pr)
        ops = [o for o in sched.ops.values() if o.process == process and (allowed is None or o.plant in allowed)
               and (not f.get("plant") or o.plant == f["plant"]) and (not f.get("sku") or o.sku == f["sku"])
               and (not f.get("order") or o.order_key == f["order"]) and (not f.get("resource") or o.resource == f["resource"])]
        ctx = Ctx(self.inp)
        rates = {(r["sku"], r["plant"], r["process"], r["resource"], r["recipe"]): r for r in val.rate_comparison(self.inp)}
        rows = []
        for o in ops:
            sk = self.inp.skus[o.sku]
            res = self.inp.resources.get(o.resource) if o.resource else None
            rt = rates.get((o.sku, o.plant, process, o.resource, o.recipe), {})
            rows.append({"op": o.id, "order": o.order_key, "sku": o.sku, "plant": o.plant, "resource": o.resource, "recipe": o.recipe,
                         "group": res.group if res else None, "gauge": sk.gauge if process == "knitting" else None,
                         "bed": sk.bed if process == "knitting" else None, "coating": sk.coating if process == "dipping" else None,
                         "family": sk.family, "customer_pack": sk.customer_pack if process == "packing" else None,
                         "qty_pairs": o.run_qty, "qty_dozens": round(o.run_qty / 12, 1), "wip_in_pairs": o.wip_in,
                         "sap_standard_rate": rt.get("sap_standard"), "demonstrated_rate": rt.get("demonstrated"), "rate_unit": rt.get("unit"),
                         "rate_basis": rt.get("basis"), "run_hours": round(o.run_hours, 2), "setup_hours": round(o.setup_hours, 2),
                         "start": o.start.isoformat(timespec="minutes") if o.start else None,
                         "finish": o.finish.isoformat(timespec="minutes") if o.finish else None, "driver": o.driver,
                         "status": "scheduled" if o.start else "blocked", "blocked_reason": o.blocked,
                         "actual_qty": None, "actual_hours": None})
        cap = an.capacity(self.inp, sched, "week", f.get("plant"), process)
        by_res = {}
        for r in cap["rows"]:
            if allowed is not None and r["plant"] not in allowed:
                continue
            x = by_res.setdefault(r["resource"], {"resource": r["resource"], "plant": r["plant"], "available": 0.0, "scheduled": 0.0, "gross": 0.0, "downtime": 0.0})
            x["available"] += r["available"]
            x["scheduled"] += r["scheduled"]
            x["gross"] += r["gross"]
            x["downtime"] += r["planned_downtime"] + r["unplanned_downtime"]
        res_rows = [{**x, "utilization_pct": round(100 * x["scheduled"] / x["available"], 1) if x["available"] else None,
                     "available": round(x["available"], 1), "scheduled": round(x["scheduled"], 1), "gross": round(x["gross"], 1),
                     "downtime": round(x["downtime"], 1)} for x in by_res.values()]
        return {"process": process, "ops": rows, "resources": res_rows,
                "actuals_note": "Actual quantities/hours are not available: no production-confirmation source is connected."}

    def gantt(self, pr, f, plan_id=None):
        plan = self._working_plan(plan_id)
        sched = self.schedule_for(plan)
        allowed = self._plants(pr)
        ops = [o for o in self.gantt_ops(sched) if (allowed is None or o["plant"] in allowed)
               and (not f.get("plant") or o["plant"] == f["plant"]) and (not f.get("process") or o["process"] == f["process"])
               and (not f.get("order") or o["order"] == f["order"]) and (not f.get("sku") or o["sku"] == f["sku"])]
        res = [r for r in self.inp.resources.values() if (allowed is None or r.plant in allowed) and (not f.get("plant") or r.plant == f["plant"])
               and (not f.get("process") or r.process == f["process"])]
        downs = [{"resource": r.id, "start": d.start.isoformat(timespec="minutes"), "end": d.end.isoformat(timespec="minutes"), "kind": d.kind, "reason": d.reason}
                 for r in res for d in r.downtimes]
        return {"plan": {k: plan[k] for k in ("id", "name", "status")}, "editable": plan["status"] == "draft",
                "horizon_start": self.inp.horizon_start.isoformat(), "ops": ops, "downtimes": downs,
                "resources": [{"id": r.id, "plant": r.plant, "process": r.process, "description": r.description} for r in res]}

    def explain(self, pr, order_key, plan_id=None):
        sched = self.schedule_for(self._working_plan(plan_id))
        e = an.explain_order(self.inp, sched, order_key)
        if "error" in e:
            raise NotFound(e["error"])
        if self._plants(pr) is not None and e["order"]["plant"] not in self._plants(pr):
            raise Forbidden("Order belongs to a plant outside your scope.")
        return e

    def capacity(self, pr, f, period, plan_id=None):
        sched = self.schedule_for(self._working_plan(plan_id))
        c = an.capacity(self.inp, sched, period, f.get("plant"), f.get("process"))
        allowed = self._plants(pr)
        c["rows"] = [r for r in c["rows"] if (allowed is None or r["plant"] in allowed) and (not f.get("resource") or r["resource"] == f["resource"])]
        rc = an.rough_cut(self.inp, sched)
        rc["resources"] = [r for r in rc["resources"] if (allowed is None or r["plant"] in allowed) and (not f.get("plant") or r["plant"] == f["plant"])
                           and (not f.get("process") or r["process"] == f["process"])]
        return {"capacity": c, "rough_cut": rc,
                "sku_load": self._sku_load(sched, allowed)}

    def _sku_load(self, sched, allowed):
        d = {}
        for o in sched.ops.values():
            if o.finish and (allowed is None or o.plant in allowed):
                x = d.setdefault((o.sku, o.process), {"sku": o.sku, "process": o.process, "hours": 0.0, "pairs": 0})
                x["hours"] += o.run_hours + o.setup_hours
                x["pairs"] += o.run_qty
        return sorted(({**x, "hours": round(x["hours"], 1)} for x in d.values()), key=lambda x: -x["hours"])

    def exceptions(self, pr, plan_id=None):
        sched = self.schedule_for(self._working_plan(plan_id))
        allowed = self._plants(pr)
        ex = an.exceptions(self.inp, sched, val.validate(self.inp), an.freshness(self.inp, self.now()))
        return {"exceptions": ex, "bottlenecks": [b for b in an.bottlenecks(self.inp, sched) if allowed is None or b["plant"] in allowed][:10]}

    def material(self, pr, plan_id=None):
        return an.material_panel(self.inp, self.schedule_for(self._working_plan(plan_id)))

    def validation(self, pr):
        f = val.validate(self.inp)
        return {"summary": val.summarise(f), "findings": f, "mapping": val.mapping_matrix(self.inp), "rates": val.rate_comparison(self.inp)}

    def refresh(self, pr):
        return {"freshness": an.freshness(self.inp, self.now()), "history": self.store.refresh_list(100),
                "sample_sources": [dataclasses.asdict(r) for r in self.inp.refresh_log if r.origin == "sample"],
                "sap_integration": {"status": "not connected", "note": "SAP adapters are interface-only (see docs/SAP_INTEGRATION.md). Uploaded files are the live path."}}

    def settings_view(self, pr):
        st = self.inp.settings
        return {"efficiency": [_ser(f) for f in st._eff.values()], "cure_rules": [_ser(r) for r in st._cure.values()],
                "freshness_policies": [_ser(p) for p in st._fresh.values()], "version": st.version,
                "planner_config": {"priority_rules": list(self.inp.config.priority_rules), "objective_weights": self.inp.config.objective_weights,
                                   "rate_kind": self.inp.config.rate_kind, "at_risk_days": self.inp.config.at_risk_days,
                                   "allow_sap_standard_fallback": self.inp.config.allow_sap_standard_fallback}}

    def audit(self, pr, limit=200, entity=None):
        self._need(pr, "view_audit")
        return {"audit": self.store.audit_list(limit, entity)}

    def plans(self, pr):
        return {"plans": self.store.list_plans()}

    def plan_detail(self, pr, pid):
        p = self.store.get_plan(int(pid))
        if not p:
            raise NotFound("Plan not found")
        return {k: p[k] for k in p if k != "decisions"}

    # ------------------------------------------------------------------ write APIs
    def generate_plan(self, pr, name=None):
        self._need(pr, "edit_schedule")
        with self.lock:
            sched = generate(self.inp)
            pid = self.store.save_plan(name or f"Draft {self.now():%Y-%m-%d %H:%M}", "plan", sched.decisions(), self._snapshot(sched), pr.user,
                                       meta={"inputs_version": self.version})
            self.store.audit(pr.user, "plan.generate", f"plan:{pid}", None, {"kpis": self.store.get_plan(pid)["snapshot"]["kpis"]}, "Automatic heuristic plan", "draft")
            log.info("plan generated", extra={"ctx": {"plan": pid, "user": pr.user}})
            return {"plan_id": pid}

    def clone_plan(self, pr, pid, reason):
        self._need(pr, "edit_schedule")
        p = self._working_plan(pid)
        with self.lock:
            sched = apply_decisions(self.inp, p["decisions"])
            new = self.store.save_plan(f"Copy of {p['name']}", "plan", sched.decisions(), self._snapshot(sched), pr.user, base_plan_id=p["id"],
                                       meta={"inputs_version": self.version})
            self.store.audit(pr.user, "plan.clone", f"plan:{new}", {"from": p["id"], "status": p["status"]}, None, reason or "clone to draft", "draft")
            return {"plan_id": new}

    def move(self, pr, plan_id, op_id, resource, not_before, reason, recipe=None):
        self._need(pr, "edit_schedule")
        p = self._working_plan(plan_id)
        with self.lock:
            sched = self.schedule_for(p)
            op = sched.ops.get(op_id)
            if op is None:
                raise NotFound("Operation not found")
            if not pr.edits_plant(op.plant):
                raise Forbidden(f"You may not edit plant {op.plant}.")
            if p["status"] != "draft":
                raise Forbidden(f"Plan {p['id']} is {p['status']} and immutable; clone it to a new draft first.")
            nb = datetime.fromisoformat(not_before) if not_before else None
            before = {"resource": op.resource, "start": op.start.isoformat() if op.start else None, "finish": op.finish.isoformat() if op.finish else None}
            new, viol, impact = apply_move(self.inp, sched, op_id, resource, nb, pr.user, reason, recipe)
            vs = [dataclasses.asdict(v) for v in viol]
            if new is None:
                self.store.audit(pr.user, "plan.override_rejected", f"op:{op_id}", before, {"requested_resource": resource, "requested_start": not_before, "violations": vs},
                                 reason or "", p["status"])
                return {"applied": False, "violations": vs, "impact": impact}
            n = new.ops[op_id]
            snap = self._snapshot(new)
            stack = self.store.kv_get(f"undo:{p['id']}", [])
            stack.append(p["decisions"])
            self.store.kv_set(f"undo:{p['id']}", stack[-20:])
            self.store.update_draft(p["id"], new.decisions(), snap)
            self._cache.pop((p["id"], self.version), None)
            self.store.audit(pr.user, "plan.override", f"op:{op_id}", before,
                             {"resource": n.resource, "start": n.start.isoformat() if n.start else None, "finish": n.finish.isoformat() if n.finish else None,
                              "downstream_changes": len(impact)}, reason, p["status"])
            return {"applied": True, "warnings": vs, "impact": impact, "kpis": snap["kpis"]}

    def undo(self, pr, plan_id):
        self._need(pr, "edit_schedule")
        p = self._working_plan(plan_id)
        if p["status"] != "draft":
            raise Forbidden("Only drafts can be changed.")
        with self.lock:
            stack = self.store.kv_get(f"undo:{p['id']}", [])
            if not stack:
                raise BadRequest("Nothing to undo.")
            dec = stack.pop()
            sched = apply_decisions(self.inp, dec)
            self.store.update_draft(p["id"], dec, self._snapshot(sched))
            self.store.kv_set(f"undo:{p['id']}", stack)
            self._cache.pop((p["id"], self.version), None)
            self.store.audit(pr.user, "plan.undo", f"plan:{p['id']}", None, {"remaining_undo": len(stack)}, "rollback of last manual change", "draft")
            return {"undone": True, "remaining": len(stack)}

    def approve(self, pr, pid, reason=""):
        self._need(pr, "approve")
        try:
            self.store.transition(int(pid), "approved", pr.user, self.four_eyes)
        except (ConflictError, KeyError) as e:
            raise BadRequest(str(e))
        self.store.audit(pr.user, "plan.approve", f"plan:{pid}", "draft", "approved", reason or "approved", "approved")
        return {"status": "approved"}

    def publish(self, pr, pid, reason=""):
        self._need(pr, "publish")
        fr = [f for f in an.freshness(self.inp, self.now()) if f["critical_warning"]]
        acks = self.store.kv_get("stale_acks", {})
        unacked = [f["source"] for f in fr if f["source"] not in acks]
        if unacked:
            raise BadRequest("Critical data is stale/failed for: " + ", ".join(unacked) +
                             ". Refresh it, or have an authorised user acknowledge it (audited) before publishing.")
        try:
            self.store.transition(int(pid), "published", pr.user)
        except (ConflictError, KeyError) as e:
            raise BadRequest(str(e))
        self.store.audit(pr.user, "plan.publish", f"plan:{pid}", "approved", "published", reason or "published", "published")
        return {"status": "published"}

    def set_override(self, pr, order_key, rank, reason):
        self._need(pr, "edit_schedule")
        if not reason or not reason.strip():
            raise BadRequest("A reason is mandatory for priority overrides.")
        o = next((x for x in self.inp.orders if x.key == order_key), None)
        if o is None:
            raise NotFound("Order not found")
        if not pr.edits_plant(o.plant):
            raise Forbidden(f"You may not edit plant {o.plant}.")
        prev = self.inp.priority_overrides.get(order_key)
        self.store.set_override(order_key, int(rank), reason.strip(), pr.user)
        self.store.audit(pr.user, "priority.override", f"order:{order_key}", prev, {"rank": rank}, reason, "n/a")
        self.rebuild()
        return {"saved": True, "note": "Regenerate the plan to apply the new priority."}

    def clear_override(self, pr, order_key, reason):
        self._need(pr, "edit_schedule")
        prev = self.inp.priority_overrides.get(order_key)
        self.store.clear_override(order_key)
        self.store.audit(pr.user, "priority.override_cleared", f"order:{order_key}", prev, None, reason or "cleared", "n/a")
        self.rebuild()
        return {"saved": True}

    def import_orders(self, pr, text, file_name, control_rows=None, control_qty=None, replace_sample=False):
        self._need(pr, "import")
        with self.lock:
            try:
                existing = {o.key for o in self.inp.orders if not (replace_sample and o.origin == "sample")}
                acc, rej, totals = importer.parse_orders(text, self.inp, existing)
            except ValueError as e:
                self.store.log_refresh("sales_orders", "upload", False, file_name, errors=[str(e)], user=pr.user)
                self.rebuild()
                raise BadRequest(str(e))
            if self._plants(pr) is not None:
                bad = [o.key for o in acc if o.plant not in self._plants(pr)]
                if bad:
                    raise Forbidden(f"File contains orders for plants outside your scope: {bad[:5]}")
            rows = len(acc) + len(rej)
            rep = importer.reconcile(rows, acc, rej, totals, int(control_rows) if control_rows not in (None, "") else None,
                                     control_qty if control_qty not in (None, "") else None)
            rep["rejected_records"] = rej
            iid = self.store.log_refresh("sales_orders", "upload", True, file_name, rows, len(acc), len(rej), rep, [], pr.user)
            self.store.save_orders(acc, iid)
            if replace_sample:
                self.store.kv_set("sample_orders_removed", True)
            self.store.audit(pr.user, "import.orders", f"file:{file_name}", None, {k: rep[k] for k in ("accepted", "rejected", "reconciled")},
                             "replace sample orders" if replace_sample else "append", "n/a")
            self.rebuild()
            return rep

    def acknowledge_stale(self, pr, source, reason):
        self._need(pr, "ack_stale")
        f = next((x for x in an.freshness(self.inp, self.now()) if x["source"] == source), None)
        if not f or not f["critical_warning"]:
            raise BadRequest("Nothing to acknowledge for that source.")
        if not f["acknowledgment_permitted"]:
            raise Forbidden(f"Policy does not permit continuing on stale '{source}' data.")
        if not reason or not reason.strip():
            raise BadRequest("A reason is required.")
        acks = self.store.kv_get("stale_acks", {})
        acks[source] = {"user": pr.user, "reason": reason, "ts": self.now().isoformat()}
        self.store.kv_set("stale_acks", acks)
        self.store.add_ack(pr.user, source, reason)
        self.store.audit(pr.user, "freshness.acknowledge", f"source:{source}", f["label"], {"affected": f["affected_decisions"]}, reason, "n/a")
        return {"acknowledged": True}

    # ------------------------------------------------------------------ scenarios
    def _baseline(self, base_plan_id=None):
        p = self._working_plan(base_plan_id) if base_plan_id else (self.store.baseline_plan() or self._working_plan())
        return p, self.schedule_for(p)

    def run_scenario(self, pr, name, changes, mode="replan", base_plan_id=None, save=True):
        self._need(pr, "run_scenarios")
        if not isinstance(changes, list) or not changes:
            raise BadRequest("Scenario needs at least one change.")
        base_plan, base_sched = self._baseline(base_plan_id)
        try:
            base = {"kpis": an.kpis(self.inp, base_sched), "orders": an.order_rows(self.inp, base_sched)}
            res = an.run_scenario(self.inp, changes, mode, base_plan["decisions"])
        except (ValueError, KeyError, StopIteration) as e:
            raise BadRequest(f"Invalid scenario: {e}")
        cmp_ = an.compare(base, res)
        out = {"baseline_plan": {"id": base_plan["id"], "name": base_plan["name"], "status": base_plan["status"]}, "comparison": cmp_,
               "violations": res["violations"], "baseline_kpis": base["kpis"], "scenario_kpis": res["kpis"]}
        if save:
            sid = self.store.save_plan(name or "Scenario", "scenario", res["decisions"],
                                       {"kpis": res["kpis"], "orders": res["orders"], "ops": self.gantt_ops(res["schedule"])}, pr.user,
                                       base_plan_id=base_plan["id"], meta={"changes": changes, "mode": mode, "comparison": cmp_})
            out["scenario_id"] = sid
            self.store.audit(pr.user, "scenario.run", f"scenario:{sid}", None, {"changes": changes, "mode": mode}, name or "", "n/a")
        return out

    def scenarios(self, pr):
        return {"scenarios": self.store.list_plans("scenario")}

    def scenario(self, pr, sid):
        p = self.store.get_plan(int(sid))
        if not p or p["kind"] != "scenario":
            raise NotFound("Scenario not found")
        return {"id": p["id"], "name": p["name"], "meta": p["meta"], "kpis": p["snapshot"]["kpis"], "created_by": p["created_by"], "created_at": p["created_at"]}

    def promote_scenario(self, pr, sid, reason):
        """Create a new DRAFT plan from a scenario's decisions. The approved baseline is not touched."""
        self._need(pr, "edit_schedule")
        p = self.store.get_plan(int(sid))
        if not p or p["kind"] != "scenario":
            raise NotFound("Scenario not found")
        raise BadRequest("Promotion of scenarios that change inputs (overtime, breakdowns, rates) requires those input changes to be approved "
                         "first; this is planned. Re-create the actions in the draft plan manually.")

    def breakdown(self, pr, resource, start, end):
        self._need(pr, "run_scenarios")
        if resource not in self.inp.resources:
            raise NotFound("Unknown resource")
        base_plan, _ = self._baseline()
        try:
            r = an.breakdown_impact(self.inp, base_plan["decisions"], resource, start, end)
        except ValueError as e:
            raise BadRequest(str(e))
        self.store.audit(pr.user, "scenario.breakdown", f"resource:{resource}", None, {"start": start, "end": end}, "impact analysis", "n/a")
        return r
