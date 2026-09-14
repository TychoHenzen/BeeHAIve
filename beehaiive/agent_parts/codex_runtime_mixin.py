from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from beehaiive.contracts import TaskContract
from beehaiive.models import RunState
from beehaiive.storage import StoreError
from beehaiive.workflow import LeaseStatus, WorkspaceLease


class CodexRuntimeMixin:
    def cancel(self: Any, problem_id: str) -> None:
        with self._lock:
            process = self._processes.get(problem_id)
            if problem_id in self._active_attempts or process is not None:
                self._cancelled.add(problem_id)
        if process is not None:
            self._terminate_process(process)

    def prepare_run(
        self: Any,
        problem_id: str,
        repository: str,
        workspace_lease: WorkspaceLease | None = None,
        validate_workspace_lease: Callable[[], None] | None = None,
    ) -> None:
        self.validate_repository(repository)
        if workspace_lease is not None:
            if workspace_lease.status is not LeaseStatus.ACTIVE:
                raise StoreError("An active workflow workspace lease is required")
            if not workspace_lease.lease_token or validate_workspace_lease is None:
                raise StoreError("A workflow workspace lease token is required")
            worktree = Path(workspace_lease.worktree_path)
            if not worktree.is_absolute() or not worktree.is_dir():
                raise StoreError("The leased worker worktree is unavailable")
            unsafe_files = tuple(
                relative
                for relative in self._repository_files()
                if not self._is_safe_repository_file(relative)
            )
            if unsafe_files:
                raise StoreError(
                    "Credential-like tracked files cannot enter a worker worktree"
                )
            validate_workspace_lease()
        with self._lock:
            if (
                problem_id in self._active_attempts
                or problem_id in self._workspace_leases
            ):
                raise StoreError("An agent execution is already prepared for this run")
            self._active_attempts.add(problem_id)
            if workspace_lease is not None:
                self._workspace_leases[problem_id] = workspace_lease
                self._workspace_validators[problem_id] = cast(
                    Callable[[], None], validate_workspace_lease
                )

    def build_task_contract(self: Any, run: RunState) -> TaskContract:
        repository = self._execution_repository_for(run.run_id)
        return TaskContract.inventory(
            run.repository,
            run.pbi_number,
            run.title,
            branch=self._discover_repository_branch(repository),
            tracked_file_count=len(self._repository_files(repository)),
            answer=run.task_answer,
        )

    def set_task_contract(self: Any, problem_id: str, contract: TaskContract) -> None:
        contract.validate()
        with self._lock:
            self._task_contracts[problem_id] = contract

    def set_session_task(self: Any, problem_id: str, task: str) -> None:
        with self._lock:
            self._session_tasks[problem_id] = task

    def set_session_event_handler(
        self: Any,
        problem_id: str,
        handler: Callable[[str, str, str | None, str], None] | None,
    ) -> None:
        with self._lock:
            if handler is None:
                self._session_event_handlers.pop(problem_id, None)
            else:
                self._session_event_handlers[problem_id] = handler

    def release_run(self: Any, problem_id: str) -> None:
        with self._lock:
            self._active_attempts.discard(problem_id)
            self._cancelled.discard(problem_id)
            self._task_contracts.pop(problem_id, None)
            self._session_tasks.pop(problem_id, None)
            self._session_event_handlers.pop(problem_id, None)
            self._workspace_leases.pop(problem_id, None)
            self._workspace_validators.pop(problem_id, None)

    def _execution_repository_for(self: Any, problem_id: str) -> Path:
        with self._lock:
            lease = self._workspace_leases.get(problem_id)
        return self.repository if lease is None else Path(lease.worktree_path)

    def validate_repository(self: Any, repository: str) -> None:
        if self.repository_name is None:
            raise StoreError(
                "Agent repository identity is not configured. Set "
                "BEEHAIIVE_AGENT_REPOSITORY_NAME."
            )
        if repository != self.repository_name:
            raise StoreError(
                f"Agent checkout {self.repository_name!r} does not match "
                f"claimed repository {repository!r}"
            )


__all__ = ["CodexRuntimeMixin"]
