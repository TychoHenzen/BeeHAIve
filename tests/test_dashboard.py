import pytest
from fastapi.testclient import TestClient

from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import ProviderError
from beehaiive.storage import OrchestratorStore, StoreError, _json_mapping
from main import (
    DashboardActionRequest,
    _dashboard_pbi,
    _execute_dashboard_action,
    create_app,
)


class DashboardProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id,
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot(
                            "owner/api",
                            1,
                            "API one",
                            metadata={
                                "subtasks": [{"id": "1a", "title": "Check API"}],
                                "readers": [
                                    {"id": "security", "status": "pass"},
                                    {"id": "tests", "status": "pending"},
                                ],
                                "reviewers": {
                                    "security": {"status": "pass"},
                                    "tests": {"status": "pending"},
                                },
                                "escalation": {
                                    "current": 1,
                                    "consecutive": 1,
                                },
                                "escalation_log": [{"tier": "terra"}],
                                "activity": [
                                    {"time": "now", "action": "Started review"}
                                ],
                            },
                        ),
                        PbiSnapshot("owner/api", 4, "API four"),
                    ),
                ),
                RepositorySnapshot(
                    "owner/web",
                    (
                        PbiSnapshot("owner/web", 2, "Web one"),
                        PbiSnapshot("owner/web", 3, "Web two"),
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


class FailingDashboardProvider(DashboardProvider):
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        raise ProviderError("dashboard provider unavailable")


def test_dashboard_projection_exposes_optional_run_details() -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "updated_at": "now",
            "event_limit": 100,
            "repositories": [
                {
                    "name": "owner/api",
                    "active": True,
                    "writer": {"run_id": "run-1", "pbi_number": 1},
                    "pbis": [
                        {
                            "id": "owner/api#1",
                            "number": 1,
                            "title": "API one",
                            "stage": "pull_request",
                            "status": "active",
                            "run_id": "run-1",
                            "events": [
                                {
                                    "type": "review",
                                    "details": {
                                        "subtasks": [
                                            {"id": "1a", "title": "Check API"}
                                        ],
                                        "readers": [
                                            {"id": "security", "status": "pass"},
                                            {"id": "tests", "status": "pending"},
                                        ],
                                        "reviewers": {
                                            "security": {"status": "pass"},
                                            "tests": {"status": "pending"},
                                        },
                                        "escalation": {
                                            "current": 1,
                                            "consecutive": 2,
                                        },
                                        "escalation_log": [{"tier": "terra"}],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    counts = view["counts"]
    pbi = view["repositories"][0]["pbis"][0]
    assert counts == {
        "projects": 1,
        "repositories": 1,
        "active_repositories": 1,
        "pbis": 1,
        "subtasks": 1,
        "writers": 1,
        "readers": 2,
        "active_runs": 1,
        "failed_runs": 0,
        "completed_runs": 0,
    }
    assert pbi["stage_label"] == "Review"
    assert pbi["subtasks"] == [{"id": "1a", "title": "Check API"}]
    assert pbi["escalation"] == {"current": 1, "consecutive": 2}
    assert pbi["escalation_log"] == [{"tier": "terra"}]

    reviewer_only = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "updated_at": "now",
            "event_limit": 100,
            "repositories": [
                {
                    "name": "owner/api",
                    "active": True,
                    "writer": None,
                    "pbis": [
                        {
                            "number": 2,
                            "stage": "backlog",
                            "events": [
                                {
                                    "type": "review",
                                    "details": {
                                        "reviewers": {"security": {"status": "pass"}}
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    assert reviewer_only["counts"]["readers"] == 1
    assert reviewer_only["repositories"][0]["pbis"][0]["readers"] == [
        {"id": "security", "status": "pass"}
    ]

    empty = build_dashboard_state(
        {"project_id": "project-1", "name": "Planning", "repositories": None}
    )
    assert empty["repositories"] == []


def test_live_dashboard_route_reports_repositories_and_writers() -> None:
    service = Orchestrator(OrchestratorStore(), DashboardProvider())
    client = TestClient(
        create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    service.synchronize("project-1")
    api_run = service.claim("project-1", "owner/api", "worker-api")
    assert api_run is not None
    service.advance(api_run.run_id, Stage.IMPLEMENT, api_run.lease_token or "")
    service.handoff(
        api_run.run_id,
        "codex/api-one",
        None,
        "Closes #1",
        api_run.lease_token or "",
    )
    service.claim("project-1", "owner/api", "worker-api")
    service.claim("project-1", "owner/web", "worker-web")

    response = client.get("/projects/project-1/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["repositories"] == 2
    assert payload["counts"]["pbis"] == 4
    assert payload["counts"]["writers"] == 2
    assert payload["counts"]["readers"] == 2
    assert payload["counts"]["subtasks"] == 1
    assert payload["counts"]["completed_runs"] == 1
    assert [repo["name"] for repo in payload["repositories"]] == [
        "owner/api",
        "owner/web",
    ]
    assert all(repo["writer"]["status"] == "active" for repo in payload["repositories"])
    api_pbi = payload["repositories"][0]["pbis"][0]
    assert api_pbi["readers"][0]["status"] == "pass"
    assert api_pbi["escalation"]["current"] == 1
    assert api_pbi["activity"][0]["action"] == "Started review"


def test_clean_dashboard_database_requires_sync_then_loads_state() -> None:
    service = Orchestrator(OrchestratorStore(), DashboardProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    before_sync = client.get("/projects/project-1/dashboard")
    assert before_sync.status_code == 409
    assert before_sync.json()["detail"] == "Unknown project: project-1"

    synced = client.post(
        "/projects/project-1/sync",
        headers={"X-API-Key": "test-key"},
    )
    assert synced.status_code == 200
    assert client.get("/projects/project-1/dashboard").status_code == 200
    service.store.close()


def test_dashboard_actions_preserve_state_and_report_results() -> None:
    service = Orchestrator(OrchestratorStore(), DashboardProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )
    auth = {"X-API-Key": "test-key"}
    service.synchronize("project-1")

    start = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "start", "approved": True, "repository": "owner/api"},
    )
    assert start.status_code == 200
    assert start.json()["action"]["status"] == "succeeded"
    run_id = start.json()["state"]["repositories"][0]["pbis"][0]["run_id"]

    busy = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "start",
            "approved": True,
            "repository": "owner/api",
            "worker_id": "second-worker",
        },
    )
    assert busy.json()["action"]["status"] == "failed"

    failed = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
        },
    )
    assert failed.status_code == 200
    assert failed.json()["action"]["status"] == "failed"
    assert failed.json()["state"]["counts"]["active_runs"] == 1

    nonexistent_pbi = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 999,
        },
    )
    assert nonexistent_pbi.status_code == 403

    approved = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert approved.json()["action"]["status"] == "succeeded"

    clarification = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
            "clarification": "Use the current repository default branch.",
        },
    )
    assert clarification.json()["action"]["status"] == "succeeded"

    stopped = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "stop",
            "approved": True,
            "repository": "owner/api",
            "run_id": run_id,
            "reason": "operator stop",
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["action"]["status"] == "succeeded"
    assert stopped.json()["state"]["counts"]["failed_runs"] == 1

    assert client.get("/projects/project-1/actions").status_code == 200
    assert service.stop(run_id).status.value == "failed"

    no_approval = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "approve"},
    )
    assert no_approval.status_code == 400

    missing_target = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "approve", "approved": True},
    )
    assert missing_target.status_code == 400

    wrong_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": "wrong-run",
        },
    )
    assert wrong_run.status_code == 403

    unauthorized_repository = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "start",
            "approved": True,
            "repository": "owner/missing",
        },
    )
    assert unauthorized_repository.status_code == 403

    missing_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "stop", "approved": True},
    )
    assert missing_run.status_code == 400

    unknown_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "stop", "approved": True, "run_id": "missing"},
    )
    assert unknown_run.status_code == 403

    mismatched_repository = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "stop",
            "approved": True,
            "repository": "owner/web",
            "run_id": run_id,
        },
    )
    assert mismatched_repository.status_code == 403

    synced = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "start", "approved": True},
    )
    assert synced.json()["action"]["status"] == "succeeded"

    with pytest.raises(StoreError, match="run_id"):
        _execute_dashboard_action(
            service,
            "project-1",
            DashboardActionRequest(action="stop", approved=True),
        )


def test_dashboard_action_failure_can_return_no_existing_state() -> None:
    service = Orchestrator(OrchestratorStore(), FailingDashboardProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={"action": "start", "approved": True},
    )

    assert response.status_code == 200
    assert response.json()["action"]["status"] == "failed"
    assert response.json()["state"] is None
    assert _dashboard_pbi(service, "project-1", "owner/api", 1) is None

    invalid_target = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
        },
    )
    assert invalid_target.status_code == 403


def test_action_store_records_lifecycle_and_validates_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()

    with pytest.raises(StoreError, match="stop reason"):
        store.stop("missing", "")
    with pytest.raises(StoreError, match="Unknown run"):
        store.stop("missing")
    with pytest.raises(StoreError, match="action project"):
        store.begin_action("", "", {})

    pending = store.begin_action("project-1", "approve", {"approved": True})
    assert pending["status"] == "pending"
    assert store.actions_for_project("project-1") == [pending]

    with pytest.raises(StoreError, match="Invalid action status"):
        store.finish_action(str(pending["id"]), "pending")
    with pytest.raises(StoreError, match="Unknown action"):
        store.finish_action("missing", "succeeded")
    with pytest.raises(StoreError, match="action_limit"):
        store.actions_for_project("project-1", 0)

    completed = store.finish_action(str(pending["id"]), "succeeded", {"approved": True})
    assert completed["status"] == "succeeded"
    assert completed["result"] == {"approved": True}

    real_connection = store._connection

    class EmptyCursor:
        def fetchone(self) -> None:
            return None

    class MissingActionRowConnection:
        def execute(self, statement: str, parameters: object = ()) -> object:
            if "SELECT * FROM actions WHERE action_id" in statement:
                return EmptyCursor()
            return real_connection.execute(statement, parameters)

        def __getattr__(self, name: str) -> object:
            return getattr(real_connection, name)

    monkeypatch.setattr(
        store,
        "_connection",
        MissingActionRowConnection(),
    )
    with pytest.raises(StoreError, match="Could not create action"):
        store.begin_action("project-1", "approve", {"approved": True})

    assert _json_mapping("") == {}
    assert _json_mapping("not-json") == {}
    assert _json_mapping("[]") == {}


def test_dashboard_runtime_assets_are_served_without_sample_data() -> None:
    service = Orchestrator(OrchestratorStore(), DashboardProvider())
    client = TestClient(create_app(orchestrator=service))

    page = client.get("/dashboard")
    script = client.get("/dashboard.js")
    client_script = client.get("/dashboard-client.mjs")

    assert page.status_code == 200
    assert "/dashboard.js" in page.text
    assert script.status_code == 200
    assert client_script.status_code == 200
    assert "SAMPLE_DATA" not in script.text
    assert "/dashboard" in script.text
    assert "Start writer" in script.text
    assert 'action: "start", repository: repository.name' in script.text
    assert 'event.action || event.type || "event"' in script.text
