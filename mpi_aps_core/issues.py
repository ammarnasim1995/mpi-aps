"""Common issue record used by every rule so the UI can list affected records and actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    record: Mapping[str, Any] = field(default_factory=dict)


def has_errors(issues) -> bool:
    return any(i.severity == ERROR for i in issues)
