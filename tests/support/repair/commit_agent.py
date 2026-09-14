from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from beehaiive.routing import AttemptOutcome, ModelExecution
from tests.support.repair.repository import git_repository


class CommitAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt = ""
        self.cancel_calls = 0
        self._secret_values: tuple[str, ...] = ()

    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        del problem_id, model, cancelled
        self.calls += 1
        self.prompt = prompt
        (worktree / "fix.txt").write_text("fixed\n", encoding="utf-8")
        git_repository(worktree, "add", "fix.txt")
        git_repository(worktree, "commit", "-m", "repair selected finding")
        return ModelExecution(AttemptOutcome.SUCCESS, result="repair committed")

    def cancel(self, _problem_id: str) -> None:
        self.cancel_calls += 1
