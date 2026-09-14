from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from beehaiive.routing import ModelExecutor
from beehaiive.workflow import WorkspaceLease


class CancellableModelExecutor(ModelExecutor, Protocol):
    """Model executor capabilities required by the asynchronous worker."""

    def cancel(self, problem_id: str) -> None: ...

    def prepare_run(
        self,
        problem_id: str,
        repository: str,
        workspace_lease: WorkspaceLease | None = None,
        validate_workspace_lease: Callable[[], None] | None = None,
    ) -> None: ...

    def release_run(self, problem_id: str) -> None: ...


__all__ = ["CancellableModelExecutor"]
