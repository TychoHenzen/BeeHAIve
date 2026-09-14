from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model_tier import ModelTier
    from .routing_status import RoutingStatus


@dataclass(frozen=True, slots=True)
class RoutingState:
    """Persisted counters and the next route for one problem."""

    problem_id: str
    status: RoutingStatus
    current_tier: ModelTier
    triage_index: int
    consecutive_failures: int
    bounce_count: int
    round: int
    total_tokens: int
    total_cost: float
    recursive_spawn_depth: int
    last_failure_context: str | None
    required_action: str | None
    next_reason: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "status": self.status.value,
            "current_tier": self.current_tier.value,
            "triage_index": self.triage_index,
            "consecutive_failures": self.consecutive_failures,
            "bounce_count": self.bounce_count,
            "round": self.round,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "recursive_spawn_depth": self.recursive_spawn_depth,
            "last_failure_context": self.last_failure_context,
            "required_action": self.required_action,
            "next_reason": self.next_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
