from __future__ import annotations

import os
from collections.abc import Callable, Collection
from contextlib import suppress
from pathlib import Path
from threading import Lock, Thread
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from beehaiive.models import RunState, RunStatus
from beehaiive.storage import StoreError
from beehaiive.workflow import WorkflowError, WorkflowService, WorkspaceLease

from .errors import WorkerCapacityError as WorkerCapacityError
from .interfaces import CancellableModelExecutor as CancellableModelExecutor
from .worker_text import redact_worker_text as redact_worker_text

if TYPE_CHECKING:
    from beehaiive.orchestrator import Orchestrator


class WorkerCapacityMixin:
    def __init__(
        self: Any,
        orchestrator: Orchestrator,
        executor: CancellableModelExecutor,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.executor = executor
        self.workflow_service = workflow_service
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._max_concurrent_workers: int | None = None
        self._workspace_leases: dict[str, WorkspaceLease] = {}
        self._workspace_validators: dict[str, Callable[[], None]] = {}
        self._delivery_lock = Lock()
        self._worker_id = f"{os.getpid()}:{uuid4().hex}"
        register = getattr(orchestrator, "register_worker_canceller", None)
        if callable(register):
            register(self.cancel)

    @property
    def active_worker_count(self: Any) -> int:
        with self._lock:
            return len(self._threads)

    def has_capacity(self: Any) -> bool:
        with self._lock:
            return (
                self._max_concurrent_workers is None
                or len(self._threads) < self._max_concurrent_workers
            )

    def set_max_concurrent_workers(self: Any, maximum: int) -> None:
        if type(maximum) is not int or maximum <= 0:
            raise StoreError("Maximum concurrent workers must be a positive integer")
        with self._lock:
            self._max_concurrent_workers = maximum

    def claim(
        self: Any,
        project_id: str,
        repository: str,
        owner_id: str,
        task: str,
        *,
        expected_run_id: str | None = None,
    ) -> RunState | None:
        if not task.strip():
            raise StoreError("An agent task is required")
        secret_values = tuple(
            value
            for value in getattr(self.executor, "_secret_values", ())
            if isinstance(value, str)
        )
        return self.orchestrator.claim(
            project_id,
            repository,
            owner_id,
            expected_run_id=expected_run_id,
            agent_session=(
                self._worker_id,
                redact_worker_text(task, secret_values, max_length=None),
            ),
        )

    def recover(
        self: Any, project_ids: Collection[str] | None = None
    ) -> tuple[str, ...]:
        service = self.workflow_service
        if service is None:
            return ()
        store = self.orchestrator.store
        recovered: list[str] = []
        for previous_run in store.active_agent_sessions():
            if project_ids is not None and previous_run.project_id not in project_ids:
                continue
            with self._lock:
                if previous_run.run_id in self._threads:
                    continue
            if not self.has_capacity():
                break
            session = store.get_agent_session(previous_run.run_id)
            task = None if session is None else session.get("task")
            if not isinstance(task, str) or not task.strip():
                task = getattr(self.executor, "task", previous_run.title)
            if not isinstance(task, str) or not task.strip():
                task = previous_run.title
            run = self.claim(
                previous_run.project_id,
                previous_run.repository,
                self._worker_id,
                task,
                expected_run_id=previous_run.run_id,
            )
            if run is None:
                continue
            lease_token = run.lease_token
            if lease_token is None:
                raise StoreError("Recovered agent run has no lease token")
            try:
                service.cleanup_dashboard_run_workspaces(run.run_id)
                self.start(run)
            except WorkerCapacityError:
                continue
            except Exception as exc:
                current = store.get_run(run.run_id)
                if (
                    current is not None
                    and current.status is RunStatus.ACTIVE
                    and current.lease_token == lease_token
                ):
                    failure = redact_worker_text(f"Agent recovery failed: {exc}")
                    try:
                        store.fail_agent_run(run.run_id, failure, lease_token)
                    except StoreError:
                        store.fail_agent_run_after_lease_loss(
                            run.run_id,
                            failure,
                            expected_lease_token=lease_token,
                        )
                continue
            recovered.append(run.run_id)
        return tuple(recovered)

    def start(self: Any, run: RunState) -> None:
        if run.status is not RunStatus.ACTIVE or run.lease_token is None:
            raise StoreError("An active leased run is required")
        if self.workflow_service is None:
            raise StoreError("Workflow service is required for a dashboard worker")
        with self._lock:
            if run.run_id in self._threads:
                raise StoreError("An agent worker is already active")
            if (
                self._max_concurrent_workers is not None
                and len(self._threads) >= self._max_concurrent_workers
            ):
                raise WorkerCapacityError("Maximum concurrent agent workers reached")
            thread = Thread(
                target=self._run,
                args=(run.run_id, run.lease_token),
                name=f"beehaiive-agent-{run.run_id[:8]}",
                daemon=True,
            )
            self._threads[run.run_id] = thread
        workspace_lease: WorkspaceLease | None = None
        executor_prepared = False
        try:
            service_repository = self.workflow_service.worktrees.repository
            executor_repository = getattr(
                self.executor, "repository", service_repository
            )
            if Path(executor_repository).resolve() != service_repository:
                raise StoreError(
                    "Agent executor and workflow service must use the same repository"
                )
            workspace_root = (
                service_repository.parent
                / f".{service_repository.name}.beehaiive"
                / "agent-worktrees"
            )
            workspace_root.mkdir(parents=True, exist_ok=True)
            workspace_id = uuid4().hex
            workspace_lease = self.workflow_service.acquire_workspace(
                f"dashboard-run:{run.run_id}",
                f"codex/beehaiive-run-{workspace_id}",
                workspace_root / workspace_id,
            )
            validate_workspace_lease = self._workspace_validator(
                run.run_id, workspace_lease
            )
            self.executor.prepare_run(
                run.run_id,
                run.repository,
                workspace_lease,
                validate_workspace_lease,
            )
            executor_prepared = True
            store = self.orchestrator.store
            previous_session = store.get_agent_session(run.run_id)
            task = (
                previous_session.get("task")
                if previous_session is not None
                else getattr(self.executor, "task", run.title)
            )
            if not isinstance(task, str) or not task.strip():
                task = run.title
            secret_values = tuple(
                value
                for value in getattr(self.executor, "_secret_values", ())
                if isinstance(value, str)
            )
            session_task = redact_worker_text(task, secret_values, max_length=None)
            store.start_agent_session(
                run.run_id,
                self._worker_id,
                session_task,
                run.lease_token,
            )
            set_session_task = getattr(self.executor, "set_session_task", None)
            if callable(set_session_task):
                set_session_task(run.run_id, session_task)
            set_event_handler = getattr(
                self.executor, "set_session_event_handler", None
            )
            if callable(set_event_handler):

                def record_event(
                    kind: str,
                    source_type: str,
                    role: str | None,
                    text: str,
                ) -> None:
                    store.record_agent_session_event(
                        run.run_id,
                        run.lease_token or "",
                        kind,
                        source_type,
                        role,
                        redact_worker_text(text, secret_values),
                    )

                set_event_handler(run.run_id, record_event)
            with self._lock:
                self._workspace_leases[run.run_id] = workspace_lease
                self._workspace_validators[run.run_id] = validate_workspace_lease
            thread.start()
        except Exception as exc:
            with self._lock:
                self._threads.pop(run.run_id, None)
                self._workspace_leases.pop(run.run_id, None)
                self._workspace_validators.pop(run.run_id, None)
            if executor_prepared:
                with suppress(Exception):
                    self.executor.release_run(run.run_id)
            if workspace_lease is not None:
                try:
                    self.workflow_service.discard_workspace(
                        workspace_lease.lease_id, "Dashboard worker failed to start"
                    )
                except WorkflowError as cleanup_error:
                    raise StoreError(
                        "Dashboard worker failed to start and workspace cleanup "
                        f"failed: {cleanup_error}"
                    ) from exc
            raise


__all__ = ["WorkerCapacityMixin"]
