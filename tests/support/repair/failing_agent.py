from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from beehaiive.routing import AttemptOutcome, ModelExecution
from tests.support.repair.commit_agent import CommitAgent


class FailingAgent(CommitAgent):
    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        del problem_id, worktree, prompt, model, cancelled
        self.calls += 1
        return ModelExecution(
            AttemptOutcome.FAILURE, failure_context="Selected repair failed"
        )
