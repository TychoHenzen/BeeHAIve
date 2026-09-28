from __future__ import annotations

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

    started_projects: list[tuple[str, str]] = []
    capacity_remaining = 1

    def start(project_id: str, repository: str) -> dict[str, object]:
        nonlocal capacity_remaining
        started_projects.append((project_id, repository))
        capacity_remaining = 0
        return {"run_id": f"run-{len(started_projects)}"}

    scheduler.autonomous_start = start
    scheduler.autonomous_has_capacity = lambda: capacity_remaining > 0
    assert scheduler.poll_once() == ("run-1",)
    assert started_projects[0] == ("project-1", "owner/a")
    assert worker.maximum == 1
    capacity_remaining = 0
    assert scheduler.poll_once() == ()

    capacity_remaining = 1
    assert scheduler.poll_once() == ("run-2",)
    assert started_projects[-1] in {
        ("project-1", "owner/b"),
        ("project-2", "owner/c"),
    }
    capacity_remaining = 1
    assert scheduler.poll_once() == ("run-3",)
    assert len(started_projects) == 3
    assert all(
        repository in {"owner/a", "owner/b", "owner/c"}
        for _project, repository in started_projects
    )
    assert orchestrator.synchronized[-2:] == ["project-1", "project-2"]
    assert scheduler.status_for("outside-allowlist") is None
    status = scheduler.status_for("project-2")
    assert status is not None
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["poll_interval_seconds"] == 600.0
    assert status["max_concurrency"] == 1
    assert status["active_workers"] == 0
    assert status["last_error"] is None


def test_poll_fills_autonomous_capacity_across_projects() -> None:
    states = {
        "project-1": {"repositories": [{"name": "owner/a", "active": True}]},
        "project-2": {"repositories": [{"name": "owner/b", "active": True}]},
    }
    scheduler, _orchestrator, worker = _scheduler(
        states, projects={"project-1", "project-2"}, maximum=2
    )
    started_projects: list[tuple[str, str]] = []

    def start(project_id: str, repository: str) -> dict[str, object]:
        started_projects.append((project_id, repository))
        return {"run_id": f"autonomous-{project_id}"}

    scheduler.autonomous_start = start
    scheduler.autonomous_has_capacity = lambda: len(started_projects) < 2

    assert scheduler.poll_once() == ("autonomous-project-1", "autonomous-project-2")
    assert started_projects == [("project-1", "owner/a"), ("project-2", "owner/b")]
    assert worker.active == 0


def test_poll_combines_worker_and_autonomous_capacity() -> None:
    states = {
        "project-1": {"repositories": [{"name": "owner/a", "active": True}]},
        "project-2": {"repositories": [{"name": "owner/b", "active": True}]},
    }
    scheduler, _orchestrator, worker = _scheduler(
        states, projects={"project-1", "project-2"}, maximum=2
    )
    worker.active = 1
    started_projects: list[tuple[str, str]] = []

    def start(project_id: str, repository: str) -> dict[str, object]:
        started_projects.append((project_id, repository))
        return {"run_id": f"autonomous-{project_id}"}

    scheduler.autonomous_start = start
    scheduler.autonomous_has_capacity = lambda: True
    scheduler.autonomous_active_count = lambda: len(started_projects)

    assert scheduler.poll_once() == ("autonomous-project-1",)
    assert started_projects == [("project-1", "owner/a")]


def test_poll_records_sync_and_autonomous_start_errors_without_secrets() -> None:
    states = {"project": {"repositories": [{"name": "owner/a", "active": True}]}}
    scheduler, orchestrator, worker = _scheduler(states)
    orchestrator.sync_error = RuntimeError("sync failed")
    assert scheduler.poll_once() == ()
    assert "RuntimeError: sync failed" in str(scheduler.status_for("project"))

    orchestrator.sync_error = None
    scheduler.autonomous_start = lambda _project, _repository: (_ for _ in ()).throw(
        RuntimeError("claim failed")
    )
    assert scheduler.poll_once() == ()
    assert "claim failed" in str(scheduler.status_for("project"))

    scheduler.autonomous_start = lambda _project, _repository: (_ for _ in ()).throw(
        RuntimeError("scheduler-secret start failed")
    )
    assert scheduler.poll_once() == ()
    status = scheduler.status_for("project")
    assert status is not None
    assert "scheduler-secret" not in str(status)
    assert status["last_started_run_ids"] == []


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


def test_poll_evaluates_building_signals_on_the_existing_cadence() -> None:
    states = {"project": {"repositories": []}}
    scheduler, _orchestrator, _worker = _scheduler(states)
    evaluations: list[tuple[str, ...]] = []
    scheduler.building_signal_poll = lambda projects: evaluations.append(
        tuple(projects)
    )

    assert scheduler.poll_once() == ()
    assert evaluations == [("project",)]
