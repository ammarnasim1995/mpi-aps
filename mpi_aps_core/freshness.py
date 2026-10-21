"""Data freshness. Distinguishes last ATTEMPTED refresh from last SUCCESSFUL refresh.

Never labels sample, estimated or stale data as live. Stale critical sources report the planning
decisions affected; acknowledgment (where policy permits) is written to the audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from .settings import SettingsStore


class Origin(str, Enum):
    LIVE_SAP = "live_sap"
    UPLOAD = "upload"
    SAMPLE = "sample"
    ESTIMATE = "estimate"


class State(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    NEVER_REFRESHED = "never_refreshed"


@dataclass(frozen=True)
class RefreshRecord:
    source: str
    origin: Origin
    attempted_at: datetime
    success: bool
    file_name: Optional[str] = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreshnessAssessment:
    source: str
    state: State
    label: str
    age_hours: Optional[Decimal]
    last_attempt_at: Optional[datetime]
    last_success_at: Optional[datetime]
    last_attempt_failed: bool
    errors: tuple[str, ...]
    critical_warning: bool
    affected_decisions: tuple[str, ...]
    acknowledgment_permitted: bool


def assess(source: str, history: list[RefreshRecord], now: datetime, settings: SettingsStore,
           process: Optional[str] = None) -> FreshnessAssessment:
    pol = settings.freshness_policy(source, process)
    recs = sorted((r for r in history if r.source == source), key=lambda r: r.attempted_at)
    last_attempt = recs[-1] if recs else None
    last_ok = next((r for r in reversed(recs) if r.success), None)
    failed = bool(last_attempt and not last_attempt.success)
    if last_ok is None:
        return FreshnessAssessment(source, State.NEVER_REFRESHED, "NO SUCCESSFUL REFRESH", None,
                                   last_attempt.attempted_at if last_attempt else None, None, failed,
                                   last_attempt.errors if last_attempt else (), pol.critical,
                                   pol.affected_decisions if pol.critical else (), pol.acknowledgment_permitted)
    age = Decimal((now - last_ok.attempted_at).total_seconds()) / Decimal(3600)
    state = State.FRESH if age <= pol.threshold_hours else State.STALE
    ts = last_ok.attempted_at.isoformat(timespec="minutes")
    if last_ok.origin == Origin.SAMPLE:
        label = f"SAMPLE DATA (not live) {ts}"
    elif last_ok.origin == Origin.ESTIMATE:
        label = f"ESTIMATE {ts}"
    elif state == State.STALE:
        label = f"STALE - last successful {last_ok.origin.value} refresh {ts}"
    elif last_ok.origin == Origin.LIVE_SAP:
        label = f"LIVE - last successful sync {ts}"
    else:
        label = f"UPLOADED {ts} ({last_ok.file_name or 'file name not recorded'})"
    stale_or_failed = state == State.STALE or failed
    return FreshnessAssessment(source, state, label, age, last_attempt.attempted_at, last_ok.attempted_at,
                               failed, last_attempt.errors if failed else (),
                               pol.critical and stale_or_failed,
                               pol.affected_decisions if pol.critical and stale_or_failed else (),
                               pol.acknowledgment_permitted)


def acknowledge_stale(settings: SettingsStore, a: FreshnessAssessment, user: str, reason: str) -> None:
    """Record an explicit acknowledgment to continue on stale data. Raises if policy forbids it."""
    if not a.critical_warning:
        raise ValueError("Nothing to acknowledge for this source.")
    if not a.acknowledgment_permitted:
        raise PermissionError(f"Policy does not permit continuing on stale '{a.source}' data.")
    settings.log_event(user, "acknowledge_stale_data", a.source, a.state.value,
                       {"last_success": a.last_success_at, "affected": a.affected_decisions}, reason)
