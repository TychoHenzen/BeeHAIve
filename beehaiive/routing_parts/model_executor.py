from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .model_execution import ModelExecution
    from .model_spec import ModelSpec
    from .routing_decision import RoutingDecision


class ModelExecutor(Protocol):
    """Boundary for invoking the model selected by the router."""

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution: ...
