import importlib

import pytest
from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import EnvironmentGitHubProvider
from beehaiive.storage import OrchestratorStore
from main import _configured_project_ids, _routing_config_from_environment, create_app
from tests.support.api_provider import ApiProvider


def test_dashboard_settings_persist_and_restore_from_state_store(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = OrchestratorStore(database)
    try:
        app = create_app(
            store=store,
            orchestrator=Orchestrator(store, ApiProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:1", "owner:7"},
        )
        with TestClient(app) as client:
            response = client.put(
                "/dashboard/settings",
                headers={"X-API-Key": "test-key"},
                json={
                    "approved": True,
                    "projects": ["owner:7"],
                    "workflow_id": "delivery-flow",
                },
            )
            assert response.status_code == 200
            assert response.json()["projects"] == ["owner:7"]
            assert response.json()["workflow_id"] == "delivery-flow"
    finally:
        store.close()

    restored_store = OrchestratorStore(database)
    try:
        restored = create_app(
            store=restored_store,
            orchestrator=Orchestrator(restored_store, ApiProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:1", "owner:7"},
        )
        with TestClient(restored) as client:
            settings = client.get("/dashboard/settings")
            assert settings.status_code == 200
            assert settings.json()["projects"] == ["owner:7"]
            assert settings.json()["workflow_id"] == "delivery-flow"
            assert settings.json()["restart_persistence"] == "server_state"
            assert client.get("/dashboard/config").json() == {"projects": ["owner:7"]}
    finally:
        restored_store.close()


def test_dashboard_settings_fail_closed_for_boundary_and_mixed_input(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "state.db")
    app = create_app(
        store=store,
        orchestrator=Orchestrator(store, ApiProvider()),
        api_key="test-key",
        allowed_project_ids={"owner:1"},
    )
    try:
        with TestClient(app) as client:
            outside = client.put(
                "/dashboard/settings",
                headers={"X-API-Key": "test-key"},
                json={"approved": True, "projects": ["owner:2"]},
            )
            assert outside.status_code == 403
            assert client.get("/dashboard/config").json() == {"projects": ["owner:1"]}
            mixed = client.put(
                "/dashboard/settings",
                headers={"X-API-Key": "test-key"},
                json={
                    "approved": True,
                    "projects": ["owner:1"],
                    "workflow_id": "Not Valid",
                },
            )
            assert mixed.status_code == 422
            assert client.get("/dashboard/config").json() == {"projects": ["owner:1"]}
    finally:
        store.close()


def test_project_scope_configuration_uses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_ALLOWED_PROJECTS", " owner:1, owner:2 ")
    assert _configured_project_ids(None) == {"owner:1", "owner:2"}

    monkeypatch.delenv("BEEHAIIVE_ALLOWED_PROJECTS")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "owner")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    assert _configured_project_ids(None) == {"owner:7"}


def test_routing_model_override_is_applied_before_worker_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_CODEX_MODEL", "configured-model")
    config = _routing_config_from_environment()

    assert config.writer.model == "configured-model"
    assert {spec.model for spec in config.triage} == {"configured-model"}


def test_owned_app_rejects_an_executor_without_worker_capabilities(
    tmp_path,
) -> None:
    class BareExecutor:
        def execute(self, spec, decision):
            del spec, decision
            return None

    store = OrchestratorStore(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError, match="cancellation"):
            create_app(
                store=store,
                model_executor=BareExecutor(),
                allowed_project_ids={"owner:1"},
            )
    finally:
        store.close()


def test_demo_mode_starts_production_app_with_bounded_adapters(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "demo")
    monkeypatch.setenv("BEEHAIIVE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("BEEHAIIVE_ROUTING_DB", str(tmp_path / "routing.db"))
    monkeypatch.setenv("BEEHAIIVE_REVIEW_DB", str(tmp_path / "review.db"))

    with TestClient(
        create_app(
            api_key="test-key",
            allowed_project_ids={"owner:1"},
            require_review_adapters=True,
        )
    ) as demo_client:
        response = demo_client.get("/")
        review_response = demo_client.post(
            "/reviews/ready",
            headers={"X-API-Key": "test-key"},
            json={"pull_request_id": "PR-1"},
        )

    assert response.status_code == 200
    assert review_response.status_code == 503
    assert review_response.json()["detail"] == (
        "Review operations are disabled in demo mode"
    )


def test_production_entrypoint_serves_live_project_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("BEEHAIIVE_SKIP_PRODUCTION_APP", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "owner")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER_TYPE", "user")
    monkeypatch.setenv("BEEHAIIVE_ALLOWED_PROJECTS", "owner:7")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "test-api-key")
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "demo")
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY_NAME", "owner/api")
    monkeypatch.setenv("BEEHAIIVE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("BEEHAIIVE_ROUTING_DB", str(tmp_path / "routing.db"))
    monkeypatch.setenv("BEEHAIIVE_REVIEW_DB", str(tmp_path / "review.db"))
    monkeypatch.setenv("BEEHAIIVE_WORKFLOW_DB", str(tmp_path / "workflow.db"))

    def discover_project(_provider: EnvironmentGitHubProvider, project_id: str):
        return ApiProvider().discover_project(project_id)

    monkeypatch.setattr(EnvironmentGitHubProvider, "discover_project", discover_project)
    import main as main_module

    reloaded = importlib.reload(main_module)
    assert reloaded.app is not None
    with TestClient(reloaded.app) as production_client:
        assert production_client.get("/dashboard").status_code == 200
        assert production_client.get("/docs").status_code == 200
        dashboard = production_client.get("/projects/owner:7/dashboard")

    assert dashboard.status_code == 200
    assert dashboard.json()["project_id"] == "owner:7"
    assert dashboard.json()["repositories"][0]["name"] == "owner/api"
