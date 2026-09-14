from __future__ import annotations

from dataclasses import dataclass, field

from .helpers import default_triage, default_writer
from .model_spec import ModelSpec
from .model_tier import ModelTier
from .routing_error import RoutingError
from .routing_limits import RoutingLimits


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Model tiers and limits used by one router instance."""

    writer: ModelSpec = field(default_factory=default_writer)
    triage: tuple[ModelSpec, ...] = field(default_factory=default_triage)
    limits: RoutingLimits = field(default_factory=RoutingLimits)

    def __post_init__(self) -> None:
        if self.writer.tier is not ModelTier.LUNA:
            raise ValueError("writer must use the Luna tier")
        triage_tiers = [spec.tier for spec in self.triage]
        if not triage_tiers:
            raise ValueError("at least one triage tier is required")
        if any(tier in {ModelTier.LUNA, ModelTier.HUMAN} for tier in triage_tiers):
            raise ValueError("triage tiers must not include Luna or Human")
        if len(set(triage_tiers)) != len(triage_tiers):
            raise ValueError("triage tiers must be unique")

    def spec_for(self, tier: ModelTier) -> ModelSpec:
        if tier is self.writer.tier:
            return self.writer
        for spec in self.triage:
            if spec.tier is tier:
                return spec
        if tier is ModelTier.HUMAN:
            return ModelSpec(ModelTier.HUMAN, "human")
        raise RoutingError(f"No model is configured for tier {tier.value}")
