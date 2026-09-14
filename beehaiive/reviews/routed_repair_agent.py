from __future__ import annotations

from pathlib import Path
from threading import Event

from ..routing import (
    ModelExecution,
    ModelSpec,
    RoutingDecision,
)
from .selected_repair_agent import SelectedRepairAgent


class RoutedRepairAgent:
    def __init__(
        self,
        agent: SelectedRepairAgent,
        attempt_id: str,
        worktree: Path,
        prompt: str,
        cancelled: Event,
    ) -> None:
        self.agent = agent
        self.attempt_id = attempt_id
        self.worktree = worktree
        self.prompt = prompt
        self.cancelled = cancelled

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        del decision
        return self.agent.execute_scoped_repair(
            self.attempt_id,
            self.worktree,
            self.prompt,
            spec.model,
            self.cancelled.is_set,
        )
