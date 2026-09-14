from __future__ import annotations

from beehaiive.agent import WorkerCapacityError
from tests.support.scheduler.helpers import create_scheduler as _scheduler


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
