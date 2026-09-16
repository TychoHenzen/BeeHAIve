import pytest
from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def test_mutating_routes_require_authentication_and_project_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )

    missing_key = project_client.post("/projects/owner:7/sync")
    wrong_project = project_client.post(
        "/projects/other:7/sync", headers={"X-API-Key": "test-key"}
    )
    project_client.post("/projects/owner:7/sync", headers={"X-API-Key": "test-key"})
    allowed_project_read = project_client.get("/projects/owner:7")
    wrong_project_read = project_client.get("/projects/other:7")
    unauthorized_repository = project_client.post(
        "/projects/owner:7/repositories/owner/other/claim",
        headers={"X-API-Key": "test-key", "X-Worker-ID": "worker-1"},
    )
    missing_worker = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers={"X-API-Key": "test-key"},
    )
    unknown_run = project_client.post(
        "/runs/missing/lease", headers={"X-API-Key": "test-key"}
    )

    monkeypatch.delenv("BEEHAIIVE_API_KEY", raising=False)
    unconfigured_client = TestClient(
        create_app(
            orchestrator=Orchestrator(OrchestratorStore(), ApiProvider()),
            allowed_project_ids={"owner:7"},
        )
    )
    unconfigured = unconfigured_client.post(
        "/projects/owner:7/sync", headers={"X-API-Key": "test-key"}
    )

    assert missing_key.status_code == 401
    assert wrong_project.status_code == 403
    assert allowed_project_read.status_code == 200
    assert wrong_project_read.status_code == 403
    assert unauthorized_repository.status_code == 403
    assert missing_worker.status_code == 401
    assert unknown_run.status_code == 403
    assert unconfigured.status_code == 503


def test_dashboard_actions_use_server_owned_key_for_same_origin_browser_requests() -> (
    None
):
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="server-key",
            allowed_project_ids={"owner:7"},
        )
    )

    browser_request = client.post(
        "/projects/owner:7/sync",
        headers={
            "X-BeeHAIve-Dashboard": "1",
            "Origin": "http://testserver",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    cross_origin_request = client.post(
        "/projects/owner:7/sync",
        headers={
            "X-BeeHAIve-Dashboard": "1",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert browser_request.status_code == 200
    assert cross_origin_request.status_code == 401
    service.store.close()
