from __future__ import annotations

from pathlib import Path

from beehaiive.routing import AttemptOutcome, ModelExecution
from tests.support.conflict_repair.helpers import git_command
from tests.support.conflict_repair.repair_agent import RepairAgent


class NonMergingRepairAgent(RepairAgent):
    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        del problem_id, source_branch, target_branch
        git_command(worktree, "merge", "--abort")
        (worktree / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        git_command(worktree, "add", "unrelated.txt")
        git_command(worktree, "commit", "-m", "unrelated repair")
        return ModelExecution(AttemptOutcome.SUCCESS, result="resolved")
