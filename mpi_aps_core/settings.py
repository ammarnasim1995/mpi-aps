"""Centralised, administrator-controlled planning settings with an audit trail.

Holds: planning-efficiency factors (hierarchy), dipping cure/processing rules, data-freshness policies.
Only APPROVED, in-effect entries are used. Every change needs an actor and a reason, is audited with
previous/new value, and bumps `version` so schedules depending on it can be flagged for recalculation.

Efficiency hierarchy (first approved match wins; multiple matches at one level = conflict, not resolved):
  1 resource  2 plant+process  3 plant  4 system default (only if explicitly approved)
Missing at every level => MISSING_EFFICIENCY; an authorised role may approve an explicit fallback.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional

from .issues import ERROR, INFO, Issue


class Scope(str, Enum):
    RESOURCE = "resource"
    PLANT_PROCESS = "plant_process"
    PLANT = "plant"
    SYSTEM_DEFAULT = "system_default"


class Approval(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EfficiencyFactor:
    scope: Scope
    factor: Decimal  # fraction of the gross rate achieved, 0 < f <= 1
    effective_from: date
    source: str
    approval_status: Approval = Approval.DRAFT
    plant: Optional[str] = None
    process: Optional[str] = None  # knitting | dipping | packing
    resource: Optional[str] = None
    effective_to: Optional[date] = None

    def key(self):
        return (self.scope, self.plant, self.process, self.resource, self.effective_from)


@dataclass(frozen=True)
class CureRule:
    """Minimum dipping-to-packing processing time. None = wildcard. More specific rule wins."""
    min_cure_hours: Decimal
    effective_from: date
    source: str
    approval_status: Approval = Approval.DRAFT
    plant: Optional[str] = None
    sku: Optional[str] = None
    recipe: Optional[str] = None
    coating: Optional[str] = None
    additional_processing_hours: Decimal = Decimal(0)
    effective_to: Optional[date] = None

    def key(self):
        return (self.plant, self.sku, self.recipe, self.coating, self.effective_from)


@dataclass(frozen=True)
class FreshnessPolicy:
    source: str
    threshold_hours: Decimal = Decimal(24)
    process: Optional[str] = None
    critical: bool = False
    affected_decisions: tuple[str, ...] = ()
    acknowledgment_permitted: bool = False


@dataclass(frozen=True)
class AuditEvent:
    user: str
    timestamp: datetime
    action: str
    entity: str
    previous: Any
    new: Any
    reason: str


@dataclass(frozen=True)
class Resolution:
    value: Optional[EfficiencyFactor]
    issues: tuple[Issue, ...]


def _in_effect(e, as_of: date) -> bool:
    return e.effective_from <= as_of and (e.effective_to is None or as_of <= e.effective_to)


class SettingsStore:
    DEFAULT_FRESHNESS_HOURS = Decimal(24)
    FALLBACK_ROLES = frozenset({"central_planning_admin", "central_planner"})

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._clock = clock
        self.version = 0
        self.audit: list[AuditEvent] = []
        self.changes: list[tuple[int, str]] = []  # (version, affected scope description)
        self._eff: dict[tuple, EfficiencyFactor] = {}
        self._cure: dict[tuple, CureRule] = {}
        self._fresh: dict[tuple, FreshnessPolicy] = {}

    # ---- write path -------------------------------------------------------------------------
    def _record(self, user, action, entity, previous, new, reason, scope_desc):
        if not user or not reason or not reason.strip():
            raise ValueError("Settings changes require a user and a non-empty reason.")
        self.version += 1
        self.audit.append(AuditEvent(user, self._clock(), action, entity, previous, new, reason.strip()))
        self.changes.append((self.version, scope_desc))

    def put_efficiency_factor(self, user: str, f: EfficiencyFactor, reason: str) -> None:
        if not (Decimal(0) < f.factor <= Decimal(1)):
            raise ValueError("Efficiency factor must satisfy 0 < factor <= 1.")
        need = {Scope.RESOURCE: ("plant", "resource"), Scope.PLANT_PROCESS: ("plant", "process"),
                Scope.PLANT: ("plant",), Scope.SYSTEM_DEFAULT: ()}[f.scope]
        missing = [n for n in need if getattr(f, n) is None]
        if missing:
            raise ValueError(f"Scope {f.scope.value} requires {missing}.")
        prev = self._eff.get(f.key())
        self._eff[f.key()] = f
        self._record(user, "put_efficiency_factor", str(f.key()), prev, f, reason,
                     f"efficiency:{f.scope.value}:{f.plant}:{f.process}:{f.resource}")

    def approve_fallback_efficiency(self, user: str, role: str, f: EfficiencyFactor, reason: str) -> None:
        """Authorised planner approves an explicitly identified fallback for missing configuration."""
        if role not in self.FALLBACK_ROLES:
            raise PermissionError(f"Role '{role}' may not approve fallback efficiency factors.")
        self.put_efficiency_factor(user, dataclasses.replace(f, approval_status=Approval.APPROVED,
                                                             source=f"planner-approved fallback: {f.source}"),
                                   reason)

    def put_cure_rule(self, user: str, r: CureRule, reason: str) -> None:
        if r.min_cure_hours < 0 or r.additional_processing_hours < 0:
            raise ValueError("Cure/processing hours cannot be negative.")
        prev = self._cure.get(r.key())
        self._cure[r.key()] = r
        self._record(user, "put_cure_rule", str(r.key()), prev, r, reason,
                     f"cure:{r.plant}:{r.sku}:{r.recipe}:{r.coating}")

    def put_freshness_policy(self, user: str, p: FreshnessPolicy, reason: str) -> None:
        if p.threshold_hours <= 0:
            raise ValueError("Freshness threshold must be positive.")
        k = (p.source, p.process)
        prev = self._fresh.get(k)
        self._fresh[k] = p
        self._record(user, "put_freshness_policy", str(k), prev, p, reason, f"freshness:{p.source}")

    def log_event(self, user, action, entity, previous, new, reason, scope_desc="event") -> None:
        """Audit-only entry (e.g. stale-data acknowledgments); does not change configuration."""
        self._record(user, action, entity, previous, new, reason, scope_desc)

    def affected_scopes_since(self, version: int) -> list[str]:
        """Scopes whose published settings changed after `version` -> schedules to recalculate."""
        return sorted({s for v, s in self.changes if v > version and not s.startswith("event")})

    # ---- read path --------------------------------------------------------------------------
    def resolve_efficiency(self, plant: str, process: str, resource: str, as_of: date) -> Resolution:
        levels = [
            (Scope.RESOURCE, lambda f: f.plant == plant and f.resource == resource
             and f.process in (None, process)),
            (Scope.PLANT_PROCESS, lambda f: f.plant == plant and f.process == process),
            (Scope.PLANT, lambda f: f.plant == plant and f.process is None),
            (Scope.SYSTEM_DEFAULT, lambda f: True),
        ]
        notes: list[Issue] = []
        ctx = {"plant": plant, "process": process, "resource": resource}
        for scope, match in levels:
            cands = [f for f in self._eff.values() if f.scope == scope and match(f) and _in_effect(f, as_of)]
            approved = [f for f in cands if f.approval_status == Approval.APPROVED]
            for f in cands:
                if f.approval_status != Approval.APPROVED:
                    notes.append(Issue("UNAPPROVED_FACTOR_IGNORED", INFO,
                                       f"{scope.value} factor {f.factor} is {f.approval_status.value}; ignored.", ctx))
            if len(approved) > 1:
                return Resolution(None, tuple(notes + [Issue("EFFICIENCY_CONFLICT", ERROR,
                                  f"{len(approved)} approved {scope.value} factors overlap; resolve in settings.", ctx)]))
            if approved:
                return Resolution(approved[0], tuple(notes))
        return Resolution(None, tuple(notes + [Issue("MISSING_EFFICIENCY", ERROR,
                          "No approved efficiency factor at resource, plant+process, plant or system-default level. "
                          "An authorised planner must configure one or approve an explicit fallback.", ctx)]))

    def resolve_cure_rule(self, plant: str, sku: str, recipe: Optional[str], coating: Optional[str],
                          as_of: date) -> tuple[Optional[CureRule], list[Issue]]:
        ctx = {"plant": plant, "sku": sku, "recipe": recipe, "coating": coating}
        subject = {"plant": plant, "sku": sku, "recipe": recipe, "coating": coating}

        def matches(r: CureRule) -> bool:
            return all(getattr(r, k) in (None, v) for k, v in subject.items())

        cands = [r for r in self._cure.values() if r.approval_status == Approval.APPROVED
                 and _in_effect(r, as_of) and matches(r)]
        if not cands:
            return None, [Issue("CURE_TIME_MISSING", ERROR,
                                "No approved cure/processing rule for this SKU/recipe/coating/plant. "
                                "Maintain it in master data; no duration is assumed.", ctx)]
        spec = lambda r: sum(getattr(r, k) is not None for k in subject)
        top = max(spec(r) for r in cands)
        best = [r for r in cands if spec(r) == top]
        if len(best) > 1:
            return None, [Issue("CURE_RULE_CONFLICT", ERROR,
                                f"{len(best)} equally specific approved cure rules apply.", ctx)]
        return best[0], []

    def freshness_policy(self, source: str, process: Optional[str] = None) -> FreshnessPolicy:
        return (self._fresh.get((source, process)) or self._fresh.get((source, None))
                or FreshnessPolicy(source, self.DEFAULT_FRESHNESS_HOURS))
