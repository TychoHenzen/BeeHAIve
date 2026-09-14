from fastapi.testclient import TestClient

import main as main_module
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
