import subprocess
import time
from pathlib import Path

import pytest
from conftest import FakeProvider
from fastapi.testclient import TestClient

import main as main_module
from beehaiive.agent import AgentWorkerManager, CodexExecModelExecutor
from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import ProviderError
from beehaiive.routing import AttemptOutcome, ModelExecution, ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore, StoreError, _json_mapping
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    LeaseStatus,
    WorkflowService,
    WorkflowStore,
)


class ImmediateDemoExecutor(CodexExecModelExecutor):
    def __init__(self, repository: Path | None = None) -> None:
        super().__init__(repository or Path.cwd(), repository_name="owner/api")

    def build_task_contract(self, run: RunState) -> TaskContract:
        return TaskContract.inventory(run.repository, run.pbi_number, run.title)

    def execute(self, spec, decision) -> ModelExecution:
        del spec, decision
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            result="demo result",
            task_result=TaskResult(TaskOutcome.PASS, {}),
        )


def make_dashboard_git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Dashboard Tests"),
    ):
        result = subprocess.run(
            ("git", *arguments), cwd=path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr or result.stdout
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=path, check=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path


def dashboard_snapshot(
    project_id: str = "project-1", api_title: str = "API one"
) -> ProjectSnapshot:
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
                        api_title,
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
                            "activity": [{"time": "now", "action": "Started review"}],
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


class FailingDashboardProvider(FakeProvider):
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
                            "task_contract": {
                                "contract_id": "test.contract",
                                "version": 1,
                                "step_id": "inspect",
                            },
                            "task_result": {
                                "outcome": "pass",
                                "evidence": {"summary": "done"},
                                "artifact_refs": [{"id": "report"}],
                            },
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
                                            "current_tier": "terra",
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
    assert pbi["escalation"] == {
        "current": 1,
        "consecutive": 2,
        "current_tier": "terra",
    }
    assert pbi["escalation_log"] == [{"tier": "terra"}]
    assert pbi["task_contract"]["contract_id"] == "test.contract"
    assert pbi["task_result"]["artifact_refs"] == [{"id": "report"}]

    completed = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [
                        {
                            "number": 1,
                            "stage": "pull_request",
                            "status": "completed",
                        }
                    ],
                }
            ],
        }
    )
    assert completed["repositories"][0]["pbis"][0]["stage_label"] == "Pull request"

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


@pytest.mark.parametrize(
    ("raw_pbi", "expected_status", "expected_stage_label"),
    (
        pytest.param(
            {
                "number": 1,
                "stage": "backlog",
                "planning_status": "Done",
                "metadata": {
                    "pull_requests": [{"number": 9, "state": "closed", "merged": True}]
                },
            },
            "idle",
            "Merged",
            id="done-with-merged-pull-request",
        ),
        pytest.param(
            {"number": 2, "stage": "backlog", "planning_status": "Blocked"},
            "idle",
            "Blocked",
            id="blocked",
        ),
        pytest.param(
            {"number": 3, "stage": "backlog", "planning_status": "New status"},
            "idle",
            "New status",
            id="unknown-status",
        ),
        pytest.param(
            {"number": 4, "stage": "implement", "planning_status": "Backlog"},
            "idle",
            "Backlog",
            id="backlog",
        ),
        pytest.param(
            {"number": 5, "stage": "pull_request", "planning_status": "Todo"},
            "idle",
            "Refine",
            id="todo",
        ),
        pytest.param(
            {
                "number": 6,
                "stage": "backlog",
                "planning_status": "In Progress",
            },
            "idle",
            "Implement",
            id="in-progress",
        ),
        pytest.param(
            {
                "number": 7,
                "stage": "implement",
                "status": "active",
                "planning_status": "Done",
            },
            "active",
            "Implement",
            id="active-run",
        ),
        pytest.param(
            {
                "number": 8,
                "stage": "implement",
                "status": "failed",
                "planning_status": "Blocked",
            },
            "failed",
            "Implement",
            id="failed-run",
        ),
        pytest.param(
            {
                "number": 9,
                "stage": "pull_request",
                "status": "completed",
                "planning_status": "Done",
            },
            "completed",
            "Pull request",
            id="completed-run",
        ),
        pytest.param(
            {"number": 10, "stage": "implement", "planning_status": "Done"},
            "idle",
            "Done",
            id="done-without-pull-request",
        ),
    ),
)
def test_dashboard_projection_separates_project_and_local_statuses(
    raw_pbi: dict[str, object],
    expected_status: str,
    expected_stage_label: str,
) -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [raw_pbi],
                }
            ],
        }
    )

    pbi = view["repositories"][0]["pbis"][0]
    assert pbi["status"] == expected_status
    assert pbi["stage_label"] == expected_stage_label
    assert pbi["planning_status"] == raw_pbi["planning_status"]


def test_dashboard_api_exposes_terminal_project_and_pull_request_state() -> None:
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "Done PBI",
                        None,
                        "Done",
                        False,
                        {
                            "pull_requests": [
                                {
                                    "number": 9,
                                    "state": "closed",
                                    "merged": True,
                                }
                            ],
                            "checks": {
                                "verdict": "unproven",
                                "pull_requests": [],
                            },
                        },
                    ),
                ),
            ),
        ),
    )
    service = Orchestrator(OrchestratorStore(), FakeProvider(snapshot))
    client = TestClient(
        main_module.create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    response = client.get("/projects/project-1/dashboard")

    assert response.status_code == 200
    pbi = response.json()["repositories"][0]["pbis"][0]
    assert pbi["status"] == "idle"
    assert pbi["planning_status"] == "Done"
    assert pbi["stage_label"] == "Merged"
    assert pbi["pull_requests"] == [{"number": 9, "state": "closed", "merged": True}]
    assert pbi["checks"] == {"verdict": "unproven", "pull_requests": []}
    service.store.close()


def test_live_dashboard_route_reports_repositories_and_writers() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(orchestrator=service, allowed_project_ids={"project-1"})
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


def test_dashboard_actions_preserve_state_and_report_results() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))

    class RecordingWorker:
        def start(self, run) -> None:
            del run

        def cancel(self, run_id: str) -> None:
            del run_id

    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            agent_worker=RecordingWorker(),
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
            "run_id": run_id,
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
            "run_id": run_id,
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

    inactive_approval = client.post(
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
    assert inactive_approval.status_code == 403

    assert client.get("/projects/project-1/actions").status_code == 200
    assert service.stop(run_id).status.value == "failed"

    no_approval = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert no_approval.status_code == 400

    missing_target = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
        },
    )
    assert missing_target.status_code == 422

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
    assert missing_run.status_code == 422

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
        "/projects/project-1/actions?archived=true",
        headers=auth,
        json={"action": "start", "approved": True},
    )
    assert synced.json()["action"]["status"] == "succeeded"

    with pytest.raises(StoreError, match="Unknown run"):
        main_module._execute_dashboard_action(
            service,
            "project-1",
            main_module.DashboardStopRequest(
                action="stop", approved=True, run_id="missing"
            ),
        )


def test_dashboard_action_failure_can_return_no_existing_state() -> None:
    service = Orchestrator(
        OrchestratorStore(), FailingDashboardProvider(dashboard_snapshot())
    )
    client = TestClient(
        main_module.create_app(
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
    assert main_module._dashboard_pbi(service, "project-1", "owner/api", 1) is None

    invalid_target = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": "missing",
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
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(main_module.create_app(orchestrator=service))

    page = client.get("/dashboard")
    script = client.get("/dashboard.js")
    client_script = client.get("/dashboard-client.mjs")
    view_script = client.get("/dashboard-view.mjs")

    assert page.status_code == 200
    assert "/dashboard.js" in page.text
    assert 'id="archived-view"' in page.text
    assert script.status_code == 200
    assert client_script.status_code == 200
    assert view_script.status_code == 200


def test_dashboard_worker_completes_bounded_demo_and_persists_result(
    tmp_path: Path,
) -> None:
    repository = make_dashboard_git_repository(tmp_path / "repository")
    executor = ImmediateDemoExecutor(repository)
    service = Orchestrator(
        OrchestratorStore(),
        FakeProvider(dashboard_snapshot()),
        ModelRouter(RoutingStore()),
        executor,
    )
    workflow_store = WorkflowStore(tmp_path / "workflow.db")

    class PassingCheck:
        name = "tests"

        def run(self, workspace: Path) -> CheckResult:
            assert workspace.exists()
            return CheckResult(self.name, True, "fixture passed")

    workflow_service = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        [PassingCheck()],
    )
    worker = AgentWorkerManager(service, executor, workflow_service)

    with TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            agent_worker=worker,
            workflow_service=workflow_service,
        )
    ) as client:
        service.synchronize("project-1")
        response = client.post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={"action": "start", "approved": True, "repository": "owner/api"},
        )

        assert response.status_code == 200
        run_id = response.json()["result"]["run"]["run_id"]
        deadline = time.monotonic() + 3
        run = service.store.get_run(run_id)
        while run is not None and run.status.value == "active":
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
            run = service.store.get_run(run_id)

        assert run is not None
        assert run.status.value == "completed"
        assert run.last_result == "demo result"
        assert run.lease_token is None
        state = client.get("/projects/project-1/dashboard").json()
        pbi = state["repositories"][0]["pbis"][0]
        assert pbi["status"] == "completed"
        assert pbi["result"] == "demo result"
        assert pbi["claimable"] is False

    lease = workflow_service.workspace_for_run(run_id)
    assert lease is not None and lease.status is LeaseStatus.RETAINED
    assert Path(lease.worktree_path).is_dir()
    workflow_service.release_workspace(lease.lease_id)
    workflow_store.close()
    service.store.close()
    service.model_router.store.close()


def test_dashboard_stop_cancels_worker_before_stopping_run() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-api")
    assert run is not None

    class RecordingWorker:
        def __init__(self) -> None:
            self.cancelled: str | None = None

        def cancel(self, run_id: str) -> None:
            self.cancelled = run_id

    worker = RecordingWorker()
    try:
        result = main_module._execute_dashboard_action(
            service,
            "project-1",
            main_module.DashboardStopRequest(
                action="stop",
                run_id=run.run_id,
                approved=True,
            ),
            worker,
        )
        assert worker.cancelled == run.run_id
        assert result["run"]["status"] == "failed"
    finally:
        service.store.close()


def test_dashboard_start_requires_a_worker_before_claiming() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    try:
        with pytest.raises(StoreError, match="worker is not configured"):
            main_module._execute_dashboard_action(
                service,
                "project-1",
                main_module.DashboardStartRequest(
                    action="start", approved=True, repository="owner/api"
                ),
            )
        assert service.store.active_runs_for_project("project-1") == ()
    finally:
        service.store.close()


def test_dashboard_start_failure_stops_claimed_run() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")

    class FailingWorker:
        def start(self, run) -> None:
            del run
            raise RuntimeError("thread start failed")

    try:
        with pytest.raises(StoreError, match="thread start failed"):
            main_module._execute_dashboard_action(
                service,
                "project-1",
                main_module.DashboardStartRequest(
                    action="start", approved=True, repository="owner/api"
                ),
                FailingWorker(),
            )
        run = service.store.active_runs_for_project("project-1")
        assert run == ()
        failed = service.store.project_state("project-1")["repositories"][0]["pbis"][0]
        assert (
            failed["last_error"] == "Agent worker failed to start: thread start failed"
        )
    finally:
        service.store.close()


def test_dashboard_start_cleanup_failure_is_reported() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")

    class FailingWorker:
        def start(self, run) -> None:
            del run
            raise RuntimeError("thread start failed")

    original_stop = service.stop

    def fail_stop(run_id: str, reason: str):
        del run_id, reason
        raise StoreError("cleanup unavailable")

    service.stop = fail_stop  # type: ignore[method-assign]
    try:
        with pytest.raises(StoreError, match="cleanup failed: cleanup unavailable"):
            main_module._execute_dashboard_action(
                service,
                "project-1",
                main_module.DashboardStartRequest(
                    action="start", approved=True, repository="owner/api"
                ),
                FailingWorker(),
            )
    finally:
        service.stop = original_stop  # type: ignore[method-assign]
        service.store.close()
