from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from beehaiive.routing import AttemptOutcome, ModelExecution
from tests.support.repair.commit_agent import CommitAgent


class BlockingAgent(CommitAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()

    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        del problem_id, worktree, model
        self.calls += 1
        self.prompt = prompt
        self.started.set()
        while cancelled is None or not cancelled():
            time.sleep(0.01)
        return ModelExecution(
            AttemptOutcome.FAILURE,
            failure_context="Agent stopped by operator",
        )
