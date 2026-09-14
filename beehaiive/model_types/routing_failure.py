from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RoutingFailure"]


@dataclass(frozen=True, slots=True)
class RoutingFailure:
    """A durable routing failure waiting for delivery to the routing store."""

    transition_id: str
    run_id: str
    error: str
    input_tokens: int
    output_tokens: int
    recursive_spawn_depth: int
