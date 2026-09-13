import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.pbi_creation import (
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationTarget,
)
from beehaiive.provider import EnvironmentGitHubProvider, ProviderError
from beehaiive.review import ReviewStore
from beehaiive.routing import RoutingStore
from beehaiive.storage import OrchestratorStore
from main import _configured_project_ids, _routing_config_from_environment, create_app

client = TestClient(create_app())


class ApiProvider:
    def __init__(self) -> None:
        self.pbi_prepare_calls = 0
        self.pbi_create_calls = 0

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id=project_id,
            name="Planning",
            repositories=(
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot("owner/api", 1, "API one"),
                        PbiSnapshot("owner/api", 2, "API two"),
                    ),
                ),
            ),
        )

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        self.pbi_prepare_calls += 1
        return PbiCreationTarget(
            project_node_id="project-node",
            repository_node_id="repository-node",
            status_field_id="status-field",
            backlog_option_id="backlog-option",
            backlog_status="Backlog",
            label_ids=tuple(f"label-{label}" for label in request.labels),
        )

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint,
    ) -> PbiCreationResult:
        self.pbi_create_calls += 1
        progress = replace(
            progress,
            issue_create_started=False,
            issue_id="issue-node",
            issue_number=3,
            issue_url="https://example.test/issues/3",
            project_item_id="project-item",
            completed_steps=(
                "issue_created",
                "labels_applied",
                "project_added",
                "status_backlog",
            ),
        )
        checkpoint(progress)
        return PbiCreationResult(
            issue_id="issue-node",
            issue_number=3,
            issue_url="https://example.test/issues/3",
            labels=request.labels,
            project_item_id="project-item",
            project_status="Backlog",
            completed_steps=progress.completed_steps,
        )


def test_root_returns_greeting() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello World"}


def test_hello_returns_name() -> None:
    response = client.get("/hello/Developer")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello Developer"}


def test_project_routes_sync_and_claim_repository_work() -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    auth_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}

    sync_response = project_client.post("/projects/owner:7/sync", headers=auth_headers)
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )

    assert sync_response.status_code == 200
    assert claim_response.status_code == 200
    assert claim_response.json()["stage"] == "refine"


def test_create_project_pbi_is_authenticated_scoped_and_idempotent() -> None:
    store = OrchestratorStore()
    provider = ApiProvider()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, provider),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    path = "/projects/owner:7/pbis"
    payload = {
        "repository": "owner/api",
        "title": "API-created PBI",
        "body": "Preserve this body.",
        "labels": ["enhancement"],
    }

    unauthenticated = project_client.post(
        path,
        headers={"Idempotency-Key": "request-1"},
        json=payload,
    )
    missing_key = project_client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json=payload,
    )
    unauthorized_project = project_client.post(
        "/projects/owner:8/pbis",
        headers={"X-API-Key": "test-key", "Idempotency-Key": "request-2"},
        json=payload,
    )
    headers = {"X-API-Key": "test-key", "Idempotency-Key": "request-3"}
    created = project_client.post(path, headers=headers, json=payload)
    replay = project_client.post(path, headers=headers, json=payload)
    changed_request = project_client.post(
        path,
        headers=headers,
        json={**payload, "body": "Changed body."},
    )

    assert unauthenticated.status_code == 401
    assert missing_key.status_code == 422
    assert unauthorized_project.status_code == 403
    assert created.status_code == 201
    assert created.json()["issue"]["url"] == "https://example.test/issues/3"
    assert created.json()["project"]["status"] == "Backlog"
    assert replay.json() == created.json()
    assert changed_request.status_code == 409
    assert provider.pbi_create_calls == 1
    store.close()


def test_create_project_pbi_returns_redacted_incomplete_result() -> None:
    class IncompleteProvider(ApiProvider):
        def create_pbi(self, request, target, progress, checkpoint):
            del request, target
            checkpoint(
                replace(
                    progress,
                    issue_create_started=False,
                    issue_id="issue-node",
                    issue_number=4,
                    issue_url="https://example.test/issues/4",
                    completed_steps=("issue_created",),
                    current_step="add_to_project",
                )
            )
            raise RuntimeError("private-provider-detail")

    store = OrchestratorStore()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, IncompleteProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    response = project_client.post(
        "/projects/owner:7/pbis",
        headers={"X-API-Key": "test-key", "Idempotency-Key": "request-incomplete"},
        json={
            "repository": "owner/api",
            "title": "API-created PBI",
            "body": "Preserve this body.",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "incomplete"
    assert response.json()["issue"]["number"] == 4
    assert response.json()["completed_steps"] == ["issue_created"]
    assert response.json()["failure"]["type"] == "RuntimeError"
    assert "private-provider-detail" not in response.text
    store.close()


def test_accept_meta_review_uses_shared_pbi_creation_service() -> None:
    store = OrchestratorStore()
    routing = RoutingStore()
    provider = ApiProvider()
    store.sync_project(
        ProjectSnapshot(
            "owner:7",
            "Planning",
            (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)),),
        )
    )
    run = store.claim_next("owner:7", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    store.ensure_task_contract(
        run.run_id,
        TaskContract.inventory("owner/api", 1, "PBI"),
        implementation.lease_token or "",
    )
    store.record_task_result(
        run.run_id,
        TaskResult(TaskOutcome.PASS, {}),
        implementation.lease_token or "",
    )
    store.complete_agent_run(run.run_id, "Completed the PBI.", lease_token)
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, provider),
            routing_store=routing,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )

    try:
        with project_client:
            headers = {"X-API-Key": "test-key"}
            reviewed = project_client.post(
                "/projects/owner:7/meta-review", headers=headers, json={}
            )
            suggestion = reviewed.json()["suggestions"][0]
            path = (
                "/projects/owner:7/meta-review/suggestions/"
                f"{suggestion['suggestion_id']}"
            )
            accepted = project_client.post(
                path, headers=headers, json={"decision": "accept"}
            )
            replay = project_client.post(
                path, headers=headers, json={"decision": "accept"}
            )

        assert reviewed.status_code == 200
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["suggestion"]["status"] == "accepted"
        assert accepted.json()["pbi_creation"]["status"] == "complete"
        assert replay.json()["pbi_creation"] == accepted.json()["pbi_creation"]
        assert provider.pbi_create_calls == 1
    finally:
        store.close()
        routing.close()


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


def test_run_routes_advance_handoff_and_fail() -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    auth_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}

    project_client.post("/projects/owner:7/sync", headers=auth_headers)
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )
    run_id = claim_response.json()["run_id"]
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim_response.json()["lease_token"],
    }

    state_response = project_client.get("/projects/owner:7")
    renew_response = project_client.post(f"/runs/{run_id}/lease", headers=lease_headers)
    advance_response = project_client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    handoff_response = project_client.post(
        f"/runs/{run_id}/handoff",
        json={"branch": "codex/api-1", "body": "Closes #1"},
        headers=lease_headers,
    )

    second_claim = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )
    failure_response = project_client.post(
        f"/runs/{second_claim.json()['run_id']}/fail",
        json={"error": "verification failed"},
        headers={
            "X-API-Key": "test-key",
            "X-Lease-Token": second_claim.json()["lease_token"],
        },
    )

    assert state_response.status_code == 200
    assert renew_response.status_code == 200
    assert advance_response.status_code == 200
    assert handoff_response.status_code == 200
    assert handoff_response.json()["stage"] == Stage.PULL_REQUEST.value
    assert failure_response.status_code == 200
    assert failure_response.json()["status"] == "failed"


class ErrorProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        raise ProviderError(f"cannot discover {project_id}")

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        raise ProviderError("cannot hand off")

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        raise ProviderError("cannot resolve base branch")

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        raise ProviderError("cannot validate handoff")


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
