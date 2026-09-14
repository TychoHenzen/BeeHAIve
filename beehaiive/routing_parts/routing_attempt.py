from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .attempt_outcome import AttemptOutcome
    from .model_tier import ModelTier


@dataclass(frozen=True, slots=True)
class RoutingAttempt:
    """One persisted model call and its accounting data."""

    attempt_id: int | None
    problem_id: str
    round: int
    model: str
    tier: ModelTier
    reason: str
    outcome: AttemptOutcome
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    bounce_count: int
    recursive_spawn_depth: int
    failure_context: str | None
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "problem_id": self.problem_id,
            "round": self.round,
            "model": self.model,
            "tier": self.tier.value,
            "reason": self.reason,
            "outcome": self.outcome.value,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "bounce_count": self.bounce_count,
            "recursive_spawn_depth": self.recursive_spawn_depth,
            "failure_context": self.failure_context,
            "created_at": self.created_at,
        }
