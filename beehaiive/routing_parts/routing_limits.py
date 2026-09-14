from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoutingLimits:
    """Hard limits that prevent an unresolved problem from running forever."""

    max_rounds: int = 8
    max_tokens: int = 64_000
    max_recursive_spawn_depth: int = 2
    max_bounces: int = 6

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.max_recursive_spawn_depth < 0:
            raise ValueError("max_recursive_spawn_depth must not be negative")
        if self.max_bounces <= 0:
            raise ValueError("max_bounces must be positive")
