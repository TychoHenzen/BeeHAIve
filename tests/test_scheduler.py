from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

import beehaiive.scheduler as scheduler_module
from beehaiive.agent import WorkerCapacityError
from beehaiive.scheduler import (
    DEFAULT_DEMO_TASK,
    SCHEDULER_ENABLED_ENV,
    SCHEDULER_MAX_CONCURRENCY_ENV,
    SCHEDULER_POLL_INTERVAL_ENV,
    AgentScheduler,
    SchedulerConfig,
)


class FakeStore:
    def __init__(self, states: dict[str, dict[str, object]]) -> None:
        self.states = states

    def project_state(self, project_id: str) -> dict[str, object]:
        return self.states[project_id]


class FakeOrchestrator:
    def __init__(self, states: dict[str, dict[str, object]]) -> None:
        self.store = FakeStore(states)
        self.synchronized: list[str] = []
        self.sync_error: Exception | None = None
        self.stopped: list[tuple[str, str]] = []
        self.stop_error: Exception | None = None

    def synchronize(self, project_id: str) -> None:
        self.synchronized.append(project_id)
        if self.sync_error is not None:
            raise self.sync_error

    def stop(self, run_id: str, reason: str) -> None:
        self.stopped.append((run_id, reason))
        if self.stop_error is not None:
            raise self.stop_error


class FakeWorker:
    def __init__(self) -> None:
        self.workflow_service = object()
        self.executor = SimpleNamespace(
            task="scheduled task", _secret_values=("scheduler-secret",)
        )
        self.maximum = 0
        self.active = 0
        self.claims: list[tuple[str, str, str, str]] = []
        self.started: list[str] = []
        self.claim_error: Exception | None = None
        self.start_error: Exception | None = None
        self.recover_error: Exception | None = None
        self.claim_result: bool = True
        self.recovered_projects: tuple[str, ...] | None = None

    @property
    def active_worker_count(self) -> int:
        return self.active

    def set_max_concurrent_workers(self, maximum: int) -> None:
        self.maximum = maximum

    def has_capacity(self) -> bool:
        return self.active < self.maximum

    def claim(
        self, project_id: str, repository: str, owner_id: str, *, task: str
    ) -> SimpleNamespace | None:
        self.claims.append((project_id, repository, owner_id, task))
        if self.claim_error is not None:
            raise self.claim_error
        if not self.claim_result:
            return None
        return SimpleNamespace(run_id=f"run-{len(self.claims)}")

    def start(self, run: SimpleNamespace) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(run.run_id)
        self.active += 1

    def recover(self, project_ids: Any = None) -> tuple[str, ...]:
        self.recovered_projects = tuple(project_ids or ())
        if self.recover_error is not None:
            raise self.recover_error
        return ()


def _scheduler(
    states: dict[str, dict[str, object]],
    worker: FakeWorker | None = None,
    projects: set[str] | None = None,
    *,
    maximum: int = 1,
) -> tuple[AgentScheduler, FakeOrchestrator, FakeWorker]:
    actual_worker = worker or FakeWorker()
    orchestrator = FakeOrchestrator(states)
    scheduler = AgentScheduler(
        orchestrator,
        actual_worker,
        projects or set(states),
        SchedulerConfig(enabled=True, max_concurrency=maximum),
    )
    return scheduler, orchestrator, actual_worker


def test_scheduler_config_parses_and_rejects_invalid_values() -> None:
    assert SchedulerConfig.from_environment({}) == SchedulerConfig()
    assert SchedulerConfig.from_environment(
        {
            SCHEDULER_ENABLED_ENV: "yes",
            SCHEDULER_POLL_INTERVAL_ENV: "1.5",
            SCHEDULER_MAX_CONCURRENCY_ENV: "3",
        }
    ) == SchedulerConfig(True, 1.5, 3)
    assert not SchedulerConfig.from_environment({SCHEDULER_ENABLED_ENV: "off"}).enabled

    invalid_values = (
        ({SCHEDULER_ENABLED_ENV: "sometimes"}, SCHEDULER_ENABLED_ENV),
        ({SCHEDULER_POLL_INTERVAL_ENV: "fast"}, SCHEDULER_POLL_INTERVAL_ENV),
        ({SCHEDULER_POLL_INTERVAL_ENV: "nan"}, SCHEDULER_POLL_INTERVAL_ENV),
        ({SCHEDULER_MAX_CONCURRENCY_ENV: "1.5"}, SCHEDULER_MAX_CONCURRENCY_ENV),
    )
    for values, message in invalid_values:
        with pytest.raises(ValueError, match=message):
            SchedulerConfig.from_environment(values)

    for values in (
        {"enabled": "true"},
        {"poll_interval_seconds": True},
        {"poll_interval_seconds": 0},
        {"max_concurrency": True},
        {"max_concurrency": 0},
    ):
        with pytest.raises(ValueError):
            SchedulerConfig(**values)  # type: ignore[arg-type]


def test_scheduler_validates_worker_project_and_enabled_state() -> None:
    states = {"project": {"repositories": []}}
    worker = FakeWorker()
    with pytest.raises(ValueError, match="allowlisted project"):
        AgentScheduler(FakeOrchestrator(states), worker, set(), SchedulerConfig(True))
    with pytest.raises(ValueError, match="must be enabled"):
        AgentScheduler(FakeOrchestrator(states), worker, {"project"}, SchedulerConfig())
    worker.workflow_service = None
    with pytest.raises(ValueError, match="workflow service"):
        AgentScheduler(
            FakeOrchestrator(states), worker, {"project"}, SchedulerConfig(True)
        )

    worker.workflow_service = object()
    worker.executor.task = " "
    scheduler = AgentScheduler(
        FakeOrchestrator(states), worker, {"project"}, SchedulerConfig(True)
    )
    assert scheduler._task == DEFAULT_DEMO_TASK


def test_poll_rotates_active_repositories_and_respects_process_capacity() -> None:
    states: dict[str, dict[str, object]] = {
        "project-1": {
            "repositories": [
                {"name": "owner/a", "active": True},
                {"name": "owner/b", "active": True},
                {"name": "owner/paused", "active": False},
                {"name": 4, "active": True},
                "invalid",
            ]
        },
        "project-2": {"repositories": [{"name": "owner/c", "active": True}]},
    }
    scheduler, orchestrator, worker = _scheduler(
        states, projects={"project-2", "project-1"}
    )

    assert scheduler.poll_once() == ("run-1",)
    assert worker.claims[0][:2] == ("project-1", "owner/a")
    assert worker.maximum == 1
    assert worker.active == 1
    assert scheduler.poll_once() == ()
    assert len(worker.claims) == 1

    worker.active = 0
    assert scheduler.poll_once() == ("run-2",)
    assert worker.claims[-1][:2] == ("project-1", "owner/b")
    worker.active = 0
    assert scheduler.poll_once() == ("run-3",)
    assert worker.claims[-1][:2] == ("project-2", "owner/c")
    assert orchestrator.synchronized[-2:] == ["project-1", "project-2"]
    assert scheduler.status_for("outside-allowlist") is None
    status = scheduler.status_for("project-2")
    assert status is not None
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["poll_interval_seconds"] == 600.0
    assert status["max_concurrency"] == 1
    assert status["active_workers"] == 1
    assert status["last_error"] is None
    assert status["last_started_run_ids"] == ["run-3"]
    assert scheduler.status_for("project-1")["last_started_run_ids"] == []


def test_poll_records_sync_claim_and_worker_start_errors_without_secrets() -> None:
    states = {"project": {"repositories": [{"name": "owner/a", "active": True}]}}
    scheduler, orchestrator, worker = _scheduler(states)
    orchestrator.sync_error = RuntimeError("sync failed")
    assert scheduler.poll_once() == ()
    assert "RuntimeError: sync failed" in str(scheduler.status_for("project"))

    orchestrator.sync_error = None
    worker.claim_error = RuntimeError("claim failed")
    assert scheduler.poll_once() == ()
    assert "claim failed" in str(scheduler.status_for("project"))

    worker.claim_error = None
    worker.start_error = RuntimeError("scheduler-secret start failed")
    orchestrator.stop_error = RuntimeError("scheduler-secret stop failed")
    assert scheduler.poll_once() == ()
    status = scheduler.status_for("project")
    assert status is not None
    assert "scheduler-secret" not in str(status)
    assert "scheduler-secret" not in str(orchestrator.stopped)
    assert status["last_started_run_ids"] == []


def test_poll_defers_a_claim_when_capacity_is_taken_before_start() -> None:
    states = {"project": {"repositories": [{"name": "owner/a", "active": True}]}}
    scheduler, orchestrator, worker = _scheduler(states)
    worker.start_error = WorkerCapacityError("Maximum concurrent agent workers reached")

    assert scheduler.poll_once() == ()
    assert len(worker.claims) == 1
    assert orchestrator.stopped == []
    assert "WorkerCapacityError" in str(scheduler.status_for("project"))


def test_poll_handles_empty_projects_and_unclaimable_repositories() -> None:
    states = {"project": {"repositories": "invalid"}}
    scheduler, _orchestrator, worker = _scheduler(states)
    worker.recover_error = RuntimeError("recovery failed")
    assert scheduler.poll_once() == ()
    assert worker.claims == []
    assert "recovery failed" in str(scheduler.status_for("project"))
    assert worker.recovered_projects == ("project",)

    worker.recover_error = None
    states["project"]["repositories"] = []
    assert scheduler.poll_once() == ()
    states["project"]["repositories"] = [{"name": "owner/a", "active": True}]
    worker.claim_result = False
    assert scheduler.poll_once() == ()
    status = scheduler.status_for("project")
    assert status is not None
    assert status["last_error"] is None
    assert status["last_started_run_ids"] == []


def test_scheduler_lifecycle_and_unexpected_poll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, _orchestrator, _worker = _scheduler({"project": {"repositories": []}})
    assert scheduler.status_for("project")["running"] is False
    scheduler.shutdown()

    failed = Event()

    def fail_poll() -> tuple[str, ...]:
        failed.set()
        scheduler._stop_event.set()
        raise RuntimeError("scheduler-secret poll failed")

    monkeypatch.setattr(scheduler, "poll_once", fail_poll)
    scheduler.start()
    assert failed.wait(2)
    scheduler.shutdown()
    status = scheduler.status_for("project")
    assert status is not None
    assert "scheduler-secret" not in str(status["last_error"])
    assert status["running"] is False


def test_scheduler_status_reports_a_running_background_thread() -> None:
    scheduler, orchestrator, _worker = _scheduler({"project": {"repositories": []}})
    entered = Event()
    release = Event()
    synchronize = orchestrator.synchronize

    def wait_during_sync(project_id: str) -> None:
        entered.set()
        assert release.wait(2)
        synchronize(project_id)

    orchestrator.synchronize = wait_during_sync
    scheduler.start()
    assert entered.wait(2)
    scheduler.start()
    assert scheduler.status_for("project")["running"] is True
    release.set()
    scheduler.shutdown()


def test_scheduler_shutdown_uses_a_bounded_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, orchestrator, _worker = _scheduler({"project": {"repositories": []}})
    entered = Event()
    release = Event()

    def wait_during_sync(project_id: str) -> None:
        del project_id
        entered.set()
        assert release.wait(1)

    monkeypatch.setattr(orchestrator, "synchronize", wait_during_sync)
    monkeypatch.setattr(
        scheduler_module, "SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=False
    )
    scheduler.start()
    assert entered.wait(1)
    with pytest.raises(TimeoutError, match="scheduler did not stop"):
        scheduler.shutdown()
    assert scheduler.status_for("project")["running"] is True
    release.set()
    scheduler.shutdown()
    assert scheduler.status_for("project")["running"] is False


def test_scheduler_start_failure_clears_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, _orchestrator, _worker = _scheduler({"project": {"repositories": []}})

    class FailedThread:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(scheduler_module, "Thread", FailedThread)
    with pytest.raises(RuntimeError, match="thread start failed"):
        scheduler.start()
    assert scheduler._thread is None
