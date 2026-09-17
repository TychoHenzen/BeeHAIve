from __future__ import annotations

from threading import Event

import pytest

from beehaiive.scheduler import (
    DEFAULT_DEMO_TASK,
    SCHEDULER_ENABLED_ENV,
    SCHEDULER_MAX_CONCURRENCY_ENV,
    SCHEDULER_POLL_INTERVAL_ENV,
    AgentScheduler,
    SchedulerConfig,
)
from tests.support.scheduler.fake_orchestrator import (
    FakeOrchestrator as FakeOrchestrator,
)
from tests.support.scheduler.fake_worker import FakeWorker as FakeWorker
from tests.support.scheduler.helpers import create_scheduler as _scheduler


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


def test_scheduler_can_be_enabled_and_reconfigured_at_runtime() -> None:
    scheduler, _orchestrator, worker = _scheduler({"project": {"repositories": []}})
    scheduler.configure(
        SchedulerConfig(enabled=False, poll_interval_seconds=30, max_concurrency=3)
    )
    assert scheduler.status_for("project")["enabled"] is False
    assert scheduler.status_for("project")["max_concurrency"] == 3
    assert worker.maximum == 3

    scheduler.configure(
        SchedulerConfig(enabled=True, poll_interval_seconds=3600, max_concurrency=2)
    )
    assert scheduler.status_for("project")["enabled"] is True
    assert scheduler.status_for("project")["running"] is True
    assert worker.maximum == 2
    scheduler.shutdown()
