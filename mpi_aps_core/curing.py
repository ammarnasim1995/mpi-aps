"""Dipping -> packing eligibility.

  earliest_packing_start = max( dipping_completion + min_cure + additional_processing,
                                quality_release_time )
Packing is blocked (None) if the cure rule is missing/conflicting or quality is not RELEASED.
Unknown quality status is never assumed released.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional

from .issues import ERROR, Issue
from .settings import SettingsStore


class QualityStatus(str, Enum):
    RELEASED = "released"
    PENDING = "pending"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PackingEligibility:
    earliest_start: Optional[datetime]
    issues: tuple[Issue, ...]


def earliest_packing_start(*, dipping_completion: datetime, plant: str, sku: str, recipe: Optional[str],
                           coating: Optional[str], quality: QualityStatus,
                           quality_released_at: Optional[datetime], settings: SettingsStore,
                           as_of: date) -> PackingEligibility:
    ctx = {"plant": plant, "sku": sku, "recipe": recipe, "coating": coating}
    rule, issues = settings.resolve_cure_rule(plant, sku, recipe, coating, as_of)
    if rule is None:
        return PackingEligibility(None, tuple(issues))
    if quality != QualityStatus.RELEASED:
        return PackingEligibility(None, (Issue("QUALITY_NOT_RELEASED", ERROR,
                                  f"Quality status is '{quality.value}'; packing not allowed.", ctx),))
    start = dipping_completion + timedelta(hours=float(rule.min_cure_hours + rule.additional_processing_hours))
    if quality_released_at is not None and quality_released_at > start:
        start = quality_released_at
    return PackingEligibility(start, ())
