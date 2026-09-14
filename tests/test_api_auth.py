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
    assert unauthorized_repository.status_code == 403
    assert missing_worker.status_code == 401
    assert unknown_run.status_code == 403
    assert unconfigured.status_code == 503
