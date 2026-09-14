from __future__ import annotations

from threading import Event
from typing import Any

import pytest

import beehaiive.scheduler_types.agent_scheduler as scheduler_module
from tests.support.scheduler.helpers import create_scheduler as _scheduler


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
