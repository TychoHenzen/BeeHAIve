from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import cast
from uuid import uuid4

from beehaiive.agent import (
    DEFAULT_DEMO_TASK,
    AgentWorkerManager,
    WorkerCapacityError,
    format_worker_exception,
    redact_worker_text,
)
from beehaiive.orchestrator import Orchestrator

from .budget import (
    AccountUsageSnapshot,
    BudgetAction,
    BudgetAdapter,
    BudgetDecision,
    BudgetEvidence,
    BudgetPolicy,
    BudgetReason,
    evaluate_budget,
)
from .constants import SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS
from .scheduler_config import SchedulerConfig

_UNSAFE_BUDGET_REASONS = frozenset(
    {
        BudgetReason.EVIDENCE_STALE,
        BudgetReason.EVIDENCE_UNAVAILABLE,
        BudgetReason.EVIDENCE_CONTRADICTORY,
        BudgetReason.BUCKETS_MISSING,
        BudgetReason.BUCKET_VALUES_INVALID,
        BudgetReason.BUDGET_EXHAUSTED,
        BudgetReason.MODEL_UNAVAILABLE,
        BudgetReason.RESET_UNVERIFIED,
    }
)

__all__ = ["AgentScheduler"]


class AgentScheduler:
    """Poll configured projects and admit one claimable writer per repository."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        worker: AgentWorkerManager,
        project_ids: set[str] | frozenset[str],
        config: SchedulerConfig,
        *,
        autonomous_start: Callable[[str, str], Mapping[str, object]] | None = None,
        autonomous_has_capacity: Callable[[], bool] | None = None,
        autonomous_set_capacity: Callable[[int], None] | None = None,
        autonomous_active_count: Callable[[], int] | None = None,
        allow_disabled: bool = False,
        budget_adapter: BudgetAdapter | None = None,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.worker = worker
        self.project_ids = tuple(sorted(project_ids))
        if not self.project_ids:
            raise ValueError("The scheduler requires at least one allowlisted project")
        if not config.enabled and not allow_disabled:
            raise ValueError("The scheduler must be enabled before it can be created")
        if worker.workflow_service is None:
            raise ValueError("A workflow service is required for scheduled workers")
        worker.set_max_concurrent_workers(config.max_concurrency)
        self.config = config
        self.autonomous_start = autonomous_start
        self.autonomous_has_capacity = autonomous_has_capacity
        self.autonomous_set_capacity = autonomous_set_capacity
        self.autonomous_active_count = autonomous_active_count
        self.budget_adapter = budget_adapter
        self.budget_policy = budget_policy or BudgetPolicy()
        self._owner_id = f"scheduler:{os.getpid()}:{uuid4().hex}"
        self._stop_event = Event()
        self._thread_lock = Lock()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._candidate_cursor: int = 0
        self._last_poll_at: str | None = None
        self._last_error: str | None = None
        self._last_started_run_ids: dict[str, tuple[str, ...]] = {}
        self._last_budget_decisions: dict[str, dict[str, object]] = {}
        executor_task = getattr(worker.executor, "task", DEFAULT_DEMO_TASK)
        self._task = (
            executor_task
            if isinstance(executor_task, str) and executor_task.strip()
            else DEFAULT_DEMO_TASK
        )

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            thread = Thread(
                target=self._run,
                name="beehaiive-agent-scheduler",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise

    def shutdown(self) -> None:
        with self._thread_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is None:
            return
        thread.join(timeout=SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise TimeoutError(
                "Agent scheduler did not stop within "
                f"{SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS:g} seconds"
            )

    def configure(self, config: SchedulerConfig) -> None:
        was_running = False
        with self._thread_lock:
            if self._thread is not None:
                was_running = self._thread.is_alive()
        self.worker.set_max_concurrent_workers(config.max_concurrency)
        if self.autonomous_set_capacity is not None:
            self.autonomous_set_capacity(config.max_concurrency)
        with self._state_lock:
            self.config = config
        if config.enabled and not was_running:
            self.start()
        elif not config.enabled and was_running:
            self.shutdown()

    def configure_projects(self, project_ids: set[str] | frozenset[str]) -> None:
        if not project_ids:
            raise ValueError("The scheduler requires at least one allowlisted project")
        with self._state_lock:
            self.project_ids = tuple(sorted(project_ids))

    def poll_once(self) -> tuple[str, ...]:
        errors: list[str] = []
        started_by_project: dict[str, list[str]] = {}
        budget_by_project: dict[str, dict[str, object]] = {}
        try:
            self.worker.recover(self.project_ids)
        except Exception as exc:
            errors.append(self._error_summary(exc))
        project_repositories: list[tuple[str, tuple[str, ...]]] = []
        for project_id in self.project_ids:
            try:
                budget = self._budget_decision(project_id)
                if budget is not None:
                    budget_by_project[project_id] = budget.as_dict()
                    if budget.action is BudgetAction.PAUSE:
                        continue
                self.orchestrator.synchronize(project_id)
                state = self.orchestrator.store.project_state(project_id)
            except Exception as exc:
                errors.append(f"{project_id}: {self._error_summary(exc)}")
                continue
            raw_repositories = state.get("repositories", [])
            if not isinstance(raw_repositories, list):
                raw_repositories = []
            repositories: list[str] = []
            for raw_repository in cast(list[object], raw_repositories):
                if not isinstance(raw_repository, dict):
                    continue
                repository = cast(dict[str, object], raw_repository)
                name = repository.get("name")
                if repository.get("active") is True and isinstance(name, str) and name:
                    repositories.append(name)
            project_repositories.append((project_id, tuple(repositories)))

        candidates = [
            (project_id, repository)
            for project_id, repositories in project_repositories
            for repository in repositories
        ]
        started: list[str] = []
        if candidates:
            start_cursor = self._candidate_cursor % len(candidates)
            attempted = False
            for offset in range(len(candidates)):
                if not self.worker.has_capacity():
                    break
                if (
                    self.autonomous_start is not None
                    and self.autonomous_active_count is not None
                    and self.worker.active_worker_count + self.autonomous_active_count()
                    >= self.config.max_concurrency
                ):
                    break
                project_id, repository = candidates[
                    (start_cursor + offset) % len(candidates)
                ]
                attempted = True
                try:
                    if self.autonomous_start is not None:
                        if (
                            self.autonomous_has_capacity is not None
                            and not self.autonomous_has_capacity()
                        ):
                            break
                        autonomous = self.autonomous_start(project_id, repository)
                        run_id = autonomous.get("run_id")
                        if not isinstance(run_id, str) or not run_id:
                            raise RuntimeError("Autonomous run did not return an id")
                        started.append(run_id)
                        started_by_project.setdefault(project_id, []).append(run_id)
                        self._candidate_cursor = (start_cursor + offset + 1) % len(
                            candidates
                        )
                        continue
                    run = self.worker.claim(
                        project_id,
                        repository,
                        self._owner_id,
                        task=self._task,
                    )
                    if run is None:
                        continue
                    try:
                        if (
                            budget_by_project.get(project_id, {}).get("action")
                            == BudgetAction.DOWNGRADE.value
                        ):
                            fallback_model = budget_by_project[project_id].get(
                                "fallback_model"
                            )
                            self.worker.start(
                                run,
                                model_override=(
                                    fallback_model
                                    if isinstance(fallback_model, str)
                                    else None
                                ),
                            )
                        else:
                            self.worker.start(run)
                    except WorkerCapacityError as exc:
                        errors.append(self._error_summary(exc))
                        continue
                    except Exception as exc:
                        error_summary = self._error_summary(exc)
                        try:
                            failure = (
                                f"Scheduled worker failed to start: {error_summary}"
                            )
                            if getattr(
                                self.orchestrator.store, "admission_enabled", False
                            ):
                                self.orchestrator.store.fail_agent_run(
                                    run.run_id, failure, run.lease_token or ""
                                )
                            else:
                                self.orchestrator.stop(run.run_id, failure)
                        except Exception as stop_error:
                            errors.append(self._error_summary(stop_error))
                        errors.append(error_summary)
                        continue
                    started.append(run.run_id)
                    started_by_project.setdefault(project_id, []).append(run.run_id)
                    self._candidate_cursor = (start_cursor + offset + 1) % len(
                        candidates
                    )
                except Exception as exc:
                    errors.append(
                        f"{project_id}/{repository}: {self._error_summary(exc)}"
                    )
            if not started and attempted:
                self._candidate_cursor = (start_cursor + 1) % len(candidates)
        self._record_poll(errors, started_by_project, budget_by_project)
        return tuple(started)

    def status_for(self, project_id: str) -> dict[str, object] | None:
        if project_id not in self.project_ids:
            return None
        with self._thread_lock:
            running = self._thread is not None and self._thread.is_alive()
        with self._state_lock:
            config = self.config
            last_poll_at = self._last_poll_at
            last_error = self._last_error
            started = self._last_started_run_ids.get(project_id, ())
            budget = self._last_budget_decisions.get(project_id)
        active_workers = self.worker.active_worker_count
        if self.autonomous_active_count is not None:
            active_workers += self.autonomous_active_count()
        status: dict[str, object] = {
            "enabled": config.enabled,
            "running": running,
            "poll_interval_seconds": config.poll_interval_seconds,
            "max_concurrency": config.max_concurrency,
            "active_workers": active_workers,
            "last_poll_at": last_poll_at,
            "last_error": last_error,
            "last_started_run_ids": list(started),
        }
        if self.budget_adapter is not None:
            status["budget"] = budget
        return status

    def _record_poll(
        self,
        errors: list[str],
        started_by_project: dict[str, list[str]],
        budget_by_project: dict[str, dict[str, object]] | None = None,
    ) -> None:
        with self._state_lock:
            self._last_poll_at = datetime.now(UTC).isoformat()
            self._last_error = errors[0] if errors else None
            self._last_started_run_ids = {
                project_id: tuple(run_ids)
                for project_id, run_ids in started_by_project.items()
            }
            self._last_budget_decisions = dict(budget_by_project or {})

    def _budget_decision(self, project_id: str) -> BudgetDecision | None:
        adapter = self.budget_adapter
        if adapter is None:
            return None
        try:
            snapshot = adapter.snapshot(project_id)
        except Exception:
            snapshot = AccountUsageSnapshot(
                source_id="budget-adapter",
                source_version="unavailable",
                observed_at=datetime.now(UTC).isoformat(),
                evidence=BudgetEvidence.UNAVAILABLE,
            )
        previous_loader = getattr(
            self.orchestrator.store, "budget_snapshot_for_project", None
        )
        decision_loader = getattr(
            self.orchestrator.store, "budget_decision_for_project", None
        )
        reset_baseline_loader = getattr(
            self.orchestrator.store, "budget_reset_baseline_for_project", None
        )
        try:
            previous_value = (
                previous_loader(project_id) if callable(previous_loader) else None
            )
            previous_decision_value = (
                decision_loader(project_id) if callable(decision_loader) else None
            )
            reset_baseline_value = (
                reset_baseline_loader(project_id)
                if callable(reset_baseline_loader)
                else None
            )
        except Exception:
            decision = BudgetDecision(
                BudgetAction.PAUSE,
                BudgetReason.EVIDENCE_CONTRADICTORY,
                snapshot.source_version,
            )
            recorder = getattr(self.orchestrator.store, "record_budget_decision", None)
            if callable(recorder):
                recorder(project_id, snapshot, decision)
            return decision
        previous = (
            previous_value if isinstance(previous_value, AccountUsageSnapshot) else None
        )
        previous_decision = (
            previous_decision_value
            if isinstance(previous_decision_value, BudgetDecision)
            else None
        )
        reset_baseline = (
            reset_baseline_value
            if isinstance(reset_baseline_value, AccountUsageSnapshot)
            else None
        )
        decision = evaluate_budget(snapshot, self.budget_policy, previous)
        if (
            previous_decision is not None
            and previous_decision.action is BudgetAction.PAUSE
            and previous_decision.reason in _UNSAFE_BUDGET_REASONS
        ):
            verified_decision = (
                evaluate_budget(snapshot, self.budget_policy, reset_baseline)
                if reset_baseline is not None
                else None
            )
            if verified_decision is not None and verified_decision.reason in {
                BudgetReason.RESET_VERIFIED,
                BudgetReason.BUDGET_LOW,
            }:
                decision = verified_decision
            elif decision.action in {BudgetAction.ALLOW, BudgetAction.DOWNGRADE}:
                decision = BudgetDecision(
                    BudgetAction.PAUSE,
                    BudgetReason.RESET_UNVERIFIED,
                    snapshot.source_version,
                )
        recorder = getattr(self.orchestrator.store, "record_budget_decision", None)
        if callable(recorder):
            recorder(project_id, snapshot, decision)
        return decision

    def _error_summary(self, error: Exception) -> str:
        secret_values = tuple(
            value
            for value in getattr(self.worker.executor, "_secret_values", ())
            if isinstance(value, str)
        )
        return redact_worker_text(
            f"{type(error).__name__}: {format_worker_exception(error)}",
            secret_values,
            max_length=16_000,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._record_poll([self._error_summary(exc)], {})
            if self._stop_event.wait(self.config.poll_interval_seconds):
                return
