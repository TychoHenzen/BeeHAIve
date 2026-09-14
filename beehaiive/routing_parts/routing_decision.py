from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model_tier import ModelTier
    from .routing_status import RoutingStatus


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The next model route and any operator action required."""

    problem_id: str
    status: RoutingStatus
    tier: ModelTier
    model: str
    reason: str
    failure_context: str | None
    requires_human: bool
    round: int
    consecutive_failures: int
    bounce_count: int
    total_tokens: int
    total_cost: float
    remaining_rounds: int
    remaining_tokens: int
    remaining_recursive_spawn_depth: int
    remaining_bounces: int

    def as_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "status": self.status.value,
            "tier": self.tier.value,
            "model": self.model,
            "reason": self.reason,
            "failure_context": self.failure_context,
            "requires_human": self.requires_human,
            "round": self.round,
            "consecutive_failures": self.consecutive_failures,
            "bounce_count": self.bounce_count,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "remaining_rounds": self.remaining_rounds,
            "remaining_tokens": self.remaining_tokens,
            "remaining_recursive_spawn_depth": self.remaining_recursive_spawn_depth,
            "remaining_bounces": self.remaining_bounces,
        }
