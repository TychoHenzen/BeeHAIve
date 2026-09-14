from fastapi.testclient import TestClient

import main as main_module
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_runtime_assets_are_served_without_sample_data() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(main_module.create_app(orchestrator=service))

    page = client.get("/dashboard")
    script = client.get("/dashboard.js")
    client_script = client.get("/dashboard-client.mjs")
    view_script = client.get("/dashboard-view.mjs")
    view_modules = {
        name: client.get(f"/dashboard-view/{name}.mjs")
        for name in ("actions", "details", "dom", "pbi", "repository", "summary")
    }

    assert page.status_code == 200
    assert "/dashboard.js" in page.text
    assert 'id="archived-view"' in page.text
    assert script.status_code == 200
    assert client_script.status_code == 200
    assert view_script.status_code == 200
    assert all(module.status_code == 200 for module in view_modules.values())
