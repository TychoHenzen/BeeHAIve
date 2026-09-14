from __future__ import annotations

from pathlib import Path

from beehaiive.routing import ModelExecution
from tests.support.conflict_repair.repair_agent import RepairAgent


class RaisingRepairAgent(RepairAgent):
    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        del problem_id, worktree, source_branch, target_branch
        raise RuntimeError("repair process crashed")
