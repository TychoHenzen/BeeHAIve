from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .check_result import CheckResult


@dataclass(frozen=True, slots=True)
class GateResult:
    """Decision and evidence for a guarded model call."""

    gate: str
    allowed: bool
    checks: tuple[CheckResult, ...]
    required_action: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "allowed": self.allowed,
            "checks": [check.as_dict() for check in self.checks],
            "required_action": self.required_action,
        }
