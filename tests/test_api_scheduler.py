from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.review import ReviewStore
from beehaiive.routing import RoutingStore
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def test_app_startup_recovers_agent_workers() -> None:
    class RecoveryProbe:
        def __init__(self) -> None:
            self.recovered = False

        def recover(self) -> None:
            self.recovered = True

        def shutdown(self) -> None:
            return None

    worker = RecoveryProbe()
    app = create_app(
        orchestrator=Orchestrator(OrchestratorStore(), ApiProvider()),
        agent_worker=worker,
    )

    with TestClient(app):
        assert worker.recovered


def test_enabled_scheduler_starts_with_recovery_and_reports_dashboard_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_POLL_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_MAX_CONCURRENCY", "2")

    class WorkerProbe:
        workflow_service = object()
        executor = SimpleNamespace(task="scheduled task")

        def __init__(self) -> None:
            self.recovered = False
            self.recovered_maximum = 0
            self.recovered_projects = ()
            self.shutdown_called = False
            self.maximum = 0

        @property
        def active_worker_count(self) -> int:
            return 0

        def recover(self, project_ids=None) -> None:
            self.recovered = True
            self.recovered_maximum = self.maximum
            self.recovered_projects = tuple(project_ids or ())

        def shutdown(self) -> None:
            self.shutdown_called = True

        def set_max_concurrent_workers(self, maximum: int) -> None:
            self.maximum = maximum

        def has_capacity(self) -> bool:
            return False

    worker = WorkerProbe()
    store = OrchestratorStore()
    service = Orchestrator(store, ApiProvider())
    app = create_app(
        orchestrator=service,
        allowed_project_ids={"owner:7"},
        agent_worker=worker,
        routing_store=RoutingStore(),
        review_store=ReviewStore(":memory:"),
    )
    with TestClient(app) as project_client:
        response = project_client.get("/projects/owner:7/dashboard")
        assert worker.recovered
        assert worker.maximum == 2
        assert worker.recovered_maximum == 2
        assert worker.recovered_projects == ("owner:7",)
        assert response.status_code == 200
        assert response.json()["scheduler"]["enabled"] is True
        assert response.json()["scheduler"]["max_concurrency"] == 2
    assert worker.shutdown_called
    store.close()


def test_enabled_scheduler_requires_worker_and_allowlisted_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_ENABLED", "true")
    routing_store = RoutingStore()
    review_store = ReviewStore(":memory:")
    try:
        with pytest.raises(ValueError, match="dashboard agent worker"):
            create_app(
                orchestrator=Orchestrator(OrchestratorStore(), ApiProvider()),
                allowed_project_ids={"owner:7"},
                routing_store=routing_store,
                review_store=review_store,
            )
        worker = SimpleNamespace(
            workflow_service=object(), executor=SimpleNamespace(task="task")
        )
        with pytest.raises(ValueError, match="allowlisted project"):
            create_app(
                orchestrator=Orchestrator(OrchestratorStore(), ApiProvider()),
                allowed_project_ids=set(),
                agent_worker=worker,
                routing_store=routing_store,
                review_store=review_store,
            )
    finally:
        routing_store.close()
        review_store.close()


def test_disabled_scheduler_status_is_visible_in_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_POLL_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_MAX_CONCURRENCY", "3")
    store = OrchestratorStore()
    app = create_app(
        orchestrator=Orchestrator(store, ApiProvider()),
        allowed_project_ids={"owner:7"},
    )
    with TestClient(app) as project_client:
        status = project_client.get("/projects/owner:7/dashboard").json()["scheduler"]
    assert status["enabled"] is False
    assert status["running"] is False
    assert status["poll_interval_seconds"] == 15.0
    assert status["max_concurrency"] == 3
    assert status["active_workers"] == 0
    store.close()


def test_disabled_scheduler_can_apply_capacity_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_SCHEDULER_ENABLED", "false")

    class WorkerProbe:
        workflow_service = object()
        executor = SimpleNamespace(task="scheduled task")

        def __init__(self) -> None:
            self.maximum = 0

        @property
        def active_worker_count(self) -> int:
            return 0

        def set_max_concurrent_workers(self, maximum: int) -> None:
            self.maximum = maximum

        def recover(self, project_ids=None) -> None:
            del project_ids

        def shutdown(self) -> None:
            return None

        def has_capacity(self) -> bool:
            return True

    store = OrchestratorStore()
    worker = WorkerProbe()
    app = create_app(
        orchestrator=Orchestrator(store, ApiProvider()),
        api_key="test-key",
        allowed_project_ids={"owner:7"},
        agent_worker=worker,
        routing_store=RoutingStore(),
        review_store=ReviewStore(":memory:"),
    )
    with TestClient(app) as project_client:
        response = project_client.post(
            "/projects/owner:7/scheduler",
            headers={"X-API-Key": "test-key"},
            json={
                "approved": True,
                "enabled": False,
                "poll_interval_seconds": 30,
                "max_concurrency": 4,
            },
        )
    assert response.status_code == 200
    assert response.json()["scheduler"]["enabled"] is False
    assert response.json()["scheduler"]["max_concurrency"] == 4
    assert worker.maximum == 4
    store.close()
