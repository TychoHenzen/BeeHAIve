"""Single-process scheduler for allowlisted repository-writer runs."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import cast
from uuid import uuid4

from .agent import (
    DEFAULT_DEMO_TASK,
    AgentWorkerManager,
    WorkerCapacityError,
    redact_worker_text,
)
from .orchestrator import Orchestrator

SCHEDULER_ENABLED_ENV = "BEEHAIIVE_SCHEDULER_ENABLED"
SCHEDULER_POLL_INTERVAL_ENV = "BEEHAIIVE_SCHEDULER_POLL_INTERVAL_SECONDS"
SCHEDULER_MAX_CONCURRENCY_ENV = "BEEHAIIVE_SCHEDULER_MAX_CONCURRENCY"
DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS = 600.0
DEFAULT_SCHEDULER_MAX_CONCURRENCY = 1
SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    enabled: bool = False
    poll_interval_seconds: float = DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS
    max_concurrency: int = DEFAULT_SCHEDULER_MAX_CONCURRENCY

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError(f"{SCHEDULER_ENABLED_ENV} must be true or false")
        if (
            type(self.poll_interval_seconds) not in {int, float}
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError(
                f"{SCHEDULER_POLL_INTERVAL_ENV} must be a finite positive number"
            )
        if type(self.max_concurrency) is not int or self.max_concurrency <= 0:
            raise ValueError(
                f"{SCHEDULER_MAX_CONCURRENCY_ENV} must be a positive integer"
            )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> SchedulerConfig:
        values = os.environ if environ is None else environ
        raw_enabled = values.get(SCHEDULER_ENABLED_ENV, "false").strip().lower()
        if raw_enabled in {"true", "1", "yes", "on"}:
            enabled = True
        elif raw_enabled in {"false", "0", "no", "off"}:
            enabled = False
        else:
            raise ValueError(f"{SCHEDULER_ENABLED_ENV} must be true or false")

        raw_interval = values.get(
            SCHEDULER_POLL_INTERVAL_ENV,
            str(DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS),
        )
        try:
            poll_interval_seconds = float(raw_interval)
        except ValueError as exc:
            raise ValueError(
                f"{SCHEDULER_POLL_INTERVAL_ENV} must be a finite positive number"
            ) from exc

        raw_concurrency = values.get(
            SCHEDULER_MAX_CONCURRENCY_ENV,
            str(DEFAULT_SCHEDULER_MAX_CONCURRENCY),
        )
        try:
            max_concurrency = int(raw_concurrency)
        except ValueError as exc:
            raise ValueError(
                f"{SCHEDULER_MAX_CONCURRENCY_ENV} must be a positive integer"
            ) from exc

        return cls(enabled, poll_interval_seconds, max_concurrency)


class AgentScheduler:
    """Poll configured projects and admit one claimable writer per repository."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        worker: AgentWorkerManager,
        project_ids: set[str] | frozenset[str],
        config: SchedulerConfig,
    ) -> None:
        self.orchestrator = orchestrator
        self.worker = worker
        self.project_ids = tuple(sorted(project_ids))
        if not self.project_ids:
            raise ValueError("The scheduler requires at least one allowlisted project")
        if not config.enabled:
            raise ValueError("The scheduler must be enabled before it can be created")
        if worker.workflow_service is None:
            raise ValueError("A workflow service is required for scheduled workers")
        worker.set_max_concurrent_workers(config.max_concurrency)
        self.config = config
        self._owner_id = f"scheduler:{os.getpid()}:{uuid4().hex}"
        self._stop_event = Event()
        self._thread_lock = Lock()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._candidate_cursor = 0
        self._last_poll_at: str | None = None
        self._last_error: str | None = None
        self._last_started_run_ids: dict[str, tuple[str, ...]] = {}
        executor_task = getattr(worker.executor, "task", DEFAULT_DEMO_TASK)
        self._task = (
            executor_task
            if isinstance(executor_task, str) and executor_task.strip()
            else DEFAULT_DEMO_TASK
        )

    def start(self) -> None:
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

    def poll_once(self) -> tuple[str, ...]:
        errors: list[str] = []
        started_by_project: dict[str, list[str]] = {}
        try:
            self.worker.recover(self.project_ids)
        except Exception as exc:
            errors.append(self._error_summary(exc))
        project_repositories: list[tuple[str, tuple[str, ...]]] = []
        for project_id in self.project_ids:
            try:
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
                project_id, repository = candidates[
                    (start_cursor + offset) % len(candidates)
                ]
                attempted = True
                try:
                    run = self.worker.claim(
                        project_id,
                        repository,
                        self._owner_id,
                        task=self._task,
                    )
                    if run is None:
                        continue
                    try:
                        self.worker.start(run)
                    except WorkerCapacityError as exc:
                        errors.append(self._error_summary(exc))
                        continue
                    except Exception as exc:
                        error_summary = self._error_summary(exc)
                        try:
                            self.orchestrator.stop(
                                run.run_id,
                                f"Scheduled worker failed to start: {error_summary}",
                            )
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
        self._record_poll(errors, started_by_project)
        return tuple(started)

    def status_for(self, project_id: str) -> dict[str, object] | None:
        if project_id not in self.project_ids:
            return None
        with self._thread_lock:
            running = self._thread is not None and self._thread.is_alive()
        with self._state_lock:
            last_poll_at = self._last_poll_at
            last_error = self._last_error
            started = self._last_started_run_ids.get(project_id, ())
        return {
            "enabled": True,
            "running": running,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "max_concurrency": self.config.max_concurrency,
            "active_workers": self.worker.active_worker_count,
            "last_poll_at": last_poll_at,
            "last_error": last_error,
            "last_started_run_ids": list(started),
        }

    def _record_poll(
        self,
        errors: list[str],
        started_by_project: dict[str, list[str]],
    ) -> None:
        with self._state_lock:
            self._last_poll_at = datetime.now(UTC).isoformat()
            self._last_error = errors[0] if errors else None
            self._last_started_run_ids = {
                project_id: tuple(run_ids)
                for project_id, run_ids in started_by_project.items()
            }

    def _error_summary(self, error: Exception) -> str:
        secret_values = tuple(
            value
            for value in getattr(self.worker.executor, "_secret_values", ())
            if isinstance(value, str)
        )
        return redact_worker_text(f"{type(error).__name__}: {error}", secret_values)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._record_poll([self._error_summary(exc)], {})
            if self._stop_event.wait(self.config.poll_interval_seconds):
                return
