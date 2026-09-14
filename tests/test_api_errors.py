from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.error_provider import ErrorProvider


def test_routes_map_store_and_provider_errors() -> None:
    error_client = TestClient(
        create_app(
            orchestrator=Orchestrator(OrchestratorStore(), ErrorProvider()),
            api_key="test-key",
            allowed_project_ids={"missing"},
        )
    )

    unknown_project = error_client.get("/projects/missing")
    provider_failure = error_client.post(
        "/projects/missing/sync", headers={"X-API-Key": "test-key"}
    )

    assert unknown_project.status_code == 409
    assert provider_failure.status_code == 502
