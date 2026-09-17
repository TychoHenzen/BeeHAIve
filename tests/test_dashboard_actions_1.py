import time

from fastapi.testclient import TestClient

import main as main_module
from beehaiive.autonomous import IDEA_CAPTURE_STEP, AutonomousLifecycleService
from beehaiive.models import PbiSnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_refresh_synchronizes_current_state() -> None:
    provider = FakeProvider(dashboard_snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    first_refresh = client.get("/projects/project-1/dashboard")
    assert first_refresh.status_code == 200
    assert first_refresh.json()["repositories"][0]["pbis"][0]["title"] == "API one"
    assert provider.discoveries == 1

    provider.snapshot = dashboard_snapshot(api_title="API latest")
    second_refresh = client.get("/projects/project-1/dashboard")
    assert second_refresh.status_code == 200
    assert second_refresh.json()["repositories"][0]["pbis"][0]["title"] == "API latest"
    assert provider.discoveries == 2
    service.store.close()


def test_dashboard_reads_require_project_allowlist_without_api_key() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    assert client.get("/projects/secret/dashboard").status_code == 403
    assert client.get("/projects/secret/actions").status_code == 403


def test_dashboard_captures_idea_for_selected_project_once() -> None:
    class CaptureExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, step, context, handover):
            assert step is IDEA_CAPTURE_STEP
            assert context["project_id"] == "project-1"
            assert context["idea"] == "Capture this"
            assert handover["single_step"] is True
            self.calls += 1
            return {
                "status": "succeeded",
                "summary": "Created Backlog issue #8.",
                "handover": {"issue_number": 8, "project_status": "Backlog"},
                "session_output": "raw output is not an action result",
            }

    store = OrchestratorStore()
    service = Orchestrator(
        store,
        FakeProvider(
            dashboard_snapshot(
                extra_pbis=(
                    PbiSnapshot("owner/api", 8, "Captured", planning_status="Backlog"),
                )
            )
        ),
    )
    executor = CaptureExecutor()
    automation = AutonomousLifecycleService(service, executor)
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            autonomous_service=automation,
        )
    )

    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={"action": "capture_idea", "approved": True, "idea": "Capture this"},
    )

    try:
        assert response.status_code == 200
        assert response.json()["action"]["status"] == "pending"
        assert response.json()["result"]["status"] == "running"
        deadline = time.monotonic() + 3
        recorded = None
        while time.monotonic() < deadline:
            actions = client.get(
                "/projects/project-1/actions",
                headers={"X-API-Key": "test-key"},
            ).json()["actions"]
            recorded = next(
                (
                    item
                    for item in actions
                    if item["id"] == response.json()["action"]["id"]
                ),
                None,
            )
            if recorded and recorded["status"] != "pending":
                break
            time.sleep(0.01)
        assert executor.calls == 1
        assert recorded is not None and recorded["status"] == "succeeded"
        assert recorded["result"]["summary"] == "Created Backlog issue #8."
        assert "session_output" not in recorded["result"]
        assert recorded["request"]["idea_key"]
        replay = client.post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={
                "action": "capture_idea",
                "approved": True,
                "idea": "Capture this",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["action"]["id"] == response.json()["action"]["id"]
        assert executor.calls == 1
        assert client.get("/dashboard/config").json() == {"projects": ["project-1"]}
    finally:
        service.store.close()
