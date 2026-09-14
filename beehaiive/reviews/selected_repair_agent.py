from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..routing import (
    ModelExecution,
)


class SelectedRepairAgent(Protocol):
    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution: ...

    def cancel(self, problem_id: str) -> None: ...
