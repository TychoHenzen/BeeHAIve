from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from beehaiive.models import RunStatus
from beehaiive.persistence.helpers.lease_helpers import _lease_is_active
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    GitDeliveryResult,
    LeaseStatus,
    WorkflowError,
    WorkspaceLease,
)

from .worker_text import redact_worker_text as redact_worker_text


class WorkerDeliveryMixin:
    def _workspace_validator(
        self: Any, run_id: str, expected: WorkspaceLease
    ) -> Callable[[], None]:
        service = self.workflow_service
        if service is None:
            raise WorkflowError("Workflow service is required for a dashboard worker")

        def validate() -> None:
            current = service.store.require_lease_token(
                expected.lease_id, expected.lease_token
            )
            if (
                current.agent_id != f"dashboard-run:{run_id}"
                or current.worktree_path != expected.worktree_path
                or current.lease_token != expected.lease_token
            ):
                raise WorkflowError("Dashboard worker workspace lease changed")

        return validate

    def cancel(self: Any, run_id: str) -> None:
        with self._lock:
            self._cancelled_runs.add(run_id)
        self.executor.cancel(run_id)

    def is_cancelled(self: Any, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled_runs

    def _is_worker_delivery(self: Any, run_id: str) -> bool:
        with self._lock:
            return run_id in self._worker_delivery_runs

    def commit_and_push(self: Any, run_id: str) -> GitDeliveryResult:
        service = self.workflow_service
        if service is None:
            raise StoreError("Workflow service is required for Git delivery")
        with self._delivery_lock:
            run = self.orchestrator.store.get_run(run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            if run.status not in {
                RunStatus.ACTIVE,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }:
                raise StoreError("Run is not eligible for Git delivery")
            if run.status is RunStatus.ACTIVE and (
                not run.lease_token
                or not _lease_is_active(getattr(run, "lease_expires_at", None))
            ):
                raise StoreError("An active run lease is required for Git delivery")
            if self._is_worker_delivery(run_id) and (
                run.status is not RunStatus.ACTIVE or not run.lease_token
            ):
                raise StoreError("Dashboard run was cancelled before Git delivery")
            if run.status is RunStatus.ACTIVE and self.is_cancelled(run_id):
                raise StoreError("Dashboard run was cancelled before Git delivery")
            lease = service.workspace_for_run(run_id)
            if lease is None:
                raise StoreError(
                    "A leased dashboard worktree is required for Git delivery"
                )
            if (
                run.status is RunStatus.ACTIVE
                and lease.status is not LeaseStatus.ACTIVE
                and not self._is_worker_delivery(run_id)
            ):
                raise StoreError(
                    "An active workspace lease is required for Git delivery"
                )
            if (
                run.status is not RunStatus.ACTIVE
                and lease.status is not LeaseStatus.RETAINED
            ):
                raise StoreError(
                    "A retained worktree is required to retry Git delivery"
                )
            service_repository = service.worktrees.repository
            executor_repository = getattr(
                self.executor, "repository", service_repository
            )
            if Path(executor_repository).resolve() != service_repository:
                raise StoreError(
                    "Agent executor and workflow service must use the same repository"
                )
            validate_repository = getattr(self.executor, "validate_repository", None)
            if callable(validate_repository):
                validate_repository(run.repository)
            expected_status = run.status
            expected_token = run.lease_token

            def validate_run() -> None:
                current = self.orchestrator.store.get_run(run_id)
                if (
                    current is None
                    or current.project_id != run.project_id
                    or current.repository != run.repository
                    or current.pbi_number != run.pbi_number
                    or current.status is not expected_status
                    or current.lease_token != expected_token
                    or (
                        expected_status is RunStatus.ACTIVE
                        and not _lease_is_active(
                            getattr(current, "lease_expires_at", None)
                        )
                    )
                    or (
                        self._is_worker_delivery(run_id)
                        and current.status is not RunStatus.ACTIVE
                    )
                    or (
                        expected_status is RunStatus.ACTIVE
                        and self.is_cancelled(run_id)
                    )
                ):
                    raise WorkflowError("Dashboard run lease changed")

            return service.commit_and_push(
                lease.lease_id,
                lease.lease_token,
                f"dashboard-run:{run_id}",
                run.repository,
                f"Implement PBI #{run.pbi_number}: {run.title}",
                validate_run,
            )

    @staticmethod
    def _delivery_summary(result: str, delivery: GitDeliveryResult) -> str:
        commit = delivery.commit_sha or "none"
        summary = (
            f"Git delivery: {delivery.status.value}; branch {delivery.branch}; "
            f"commit {commit}. {delivery.evidence}"
        )
        return redact_worker_text(f"{result}\n{summary}")

    def shutdown(self: Any) -> None:
        stop_host_heartbeat = getattr(self, "_stop_host_heartbeat", None)
        if callable(stop_host_heartbeat):
            stop_host_heartbeat()
        with self._lock:
            items = list(self._threads.items())
        cancellation_error: Exception | None = None
        for run_id, _thread in items:
            try:
                self.cancel(run_id)
            except Exception as exc:
                if cancellation_error is None:
                    cancellation_error = exc
        for _run_id, thread in items:
            thread.join(timeout=5)
            is_alive = getattr(thread, "is_alive", lambda: False)
            if is_alive():
                thread.join(timeout=1)
        live_workers = [
            run_id
            for run_id, thread in items
            if getattr(thread, "is_alive", lambda: False)()
        ]
        if cancellation_error is not None:
            raise cancellation_error
        if live_workers:
            raise StoreError(
                "Agent workers did not stop before shutdown: " + ", ".join(live_workers)
            )
        for run_id, _thread in items:
            run = self.orchestrator.store.get_run(run_id)
            if run is not None and run.status is RunStatus.ACTIVE:
                with suppress(StoreError):
                    self.orchestrator.stop(run_id, "Agent worker shut down")
        if self.workflow_service is not None:
            for run_id, _thread in items:
                self.workflow_service.cleanup_dashboard_run_workspaces(run_id)


__all__ = ["WorkerDeliveryMixin"]
