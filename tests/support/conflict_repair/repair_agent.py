from __future__ import annotations

from pathlib import Path

from beehaiive.routing import AttemptOutcome, ModelExecution
from tests.support.conflict_repair.helpers import git_command


class RepairAgent:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls = 0

    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        del problem_id, source_branch, target_branch
        self.calls += 1
        if not self.succeed:
            return ModelExecution(
                AttemptOutcome.FAILURE, failure_context="agent could not resolve"
            )
        (worktree / "README.md").write_text("resolved\n", encoding="utf-8")
        git_command(worktree, "add", "README.md")
        git_command(worktree, "commit", "-m", "repair conflict")
        return ModelExecution(AttemptOutcome.SUCCESS, result="resolved")
