from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..contracts import TaskResult

if TYPE_CHECKING:
    from .routing_attempt import RoutingAttempt
    from .routing_decision import RoutingDecision
    from .routing_state import RoutingState


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """API-ready view of one routing transition."""

    state: RoutingState
    decision: RoutingDecision
    attempt: RoutingAttempt | None
    attempts: tuple[RoutingAttempt, ...]
    execution_result: str | None = None
    task_result: TaskResult | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.as_dict(),
            "decision": self.decision.as_dict(),
            "attempt": None if self.attempt is None else self.attempt.as_dict(),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "task_result": (
                None if self.task_result is None else self.task_result.as_dict()
            ),
        }
