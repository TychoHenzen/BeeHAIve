from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .deterministic_check_runner import DeterministicCheckRunner
from .git_worktree_manager import GitWorktreeManager
from .workflow_error import WorkflowError
from .workflow_service_checks_mixin import WorkflowServiceChecksMixin
from .workflow_service_delivery_mixin import WorkflowServiceDeliveryMixin
from .workflow_service_handoff_mixin import WorkflowServiceHandoffMixin
from .workflow_service_repair_mixin import WorkflowServiceRepairMixin
from .workflow_service_workspace_mixin import WorkflowServiceWorkspaceMixin

if TYPE_CHECKING:
    from .check_suite import CheckSuite
    from .constitution import Constitution
    from .deterministic_check import DeterministicCheck
    from .workflow_store import WorkflowStore


class WorkflowService(
    WorkflowServiceWorkspaceMixin,
    WorkflowServiceDeliveryMixin,
    WorkflowServiceRepairMixin,
    WorkflowServiceHandoffMixin,
    WorkflowServiceChecksMixin,
):
    def __init__(
        self,
        store: WorkflowStore,
        repository: str | Path,
        constitution: Constitution,
        checks: Iterable[DeterministicCheck] | None,
        worktrees: GitWorktreeManager | None = None,
        check_runner: CheckSuite | None = None,
    ) -> None:
        if checks is not None and check_runner is not None:
            raise WorkflowError("Provide checks or a check runner, not both")
        if check_runner is None:
            if checks is None:
                raise WorkflowError(
                    "A check runner or deterministic checks are required"
                )
            check_runner = DeterministicCheckRunner(checks)
        self.store = store
        self.constitution = constitution
        self.checks = check_runner
        self.worktrees = worktrees or GitWorktreeManager(repository, store)
