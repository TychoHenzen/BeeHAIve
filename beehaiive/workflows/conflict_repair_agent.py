from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..routing import ModelExecution


class ConflictRepairAgent(Protocol):
    """Bounded write-enabled agent used only inside a leased worktree."""

    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution: ...
