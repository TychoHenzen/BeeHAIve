from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from beehaiive.routing import ModelExecution
from tests.support.repair.commit_agent import CommitAgent
from tests.support.repair.repository import git_repository


class RemoteChangingAgent(CommitAgent):
    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        result = super().execute_scoped_repair(
            problem_id, worktree, prompt, model, cancelled
        )
        git_repository(
            worktree, "remote", "set-url", "--push", "origin", "owner/other.git"
        )
        return result
