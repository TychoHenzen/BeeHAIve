import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main as main_module
from beehaiive.agent import AgentWorkerManager
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    LeaseStatus,
    WorkflowService,
    WorkflowStore,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import (
    dashboard_snapshot,
    make_dashboard_git_repository,
)
from tests.support.dashboard.immediate_demo_executor import (
    ImmediateDemoExecutor as ImmediateDemoExecutor,
)


def test_dashboard_delivery_rejects_a_run_outside_the_requested_scope() -> None:
    orchestrator = SimpleNamespace(store=SimpleNamespace(get_run=lambda _run_id: None))
    with pytest.raises(main_module.HTTPException) as raised:
        main_module._require_dashboard_delivery_run(
            orchestrator,
            "project-1",
            "owner/api",
            40,
            "missing-run",
        )
    assert raised.value.status_code == 403


def test_dashboard_commit_push_requires_a_worker() -> None:
    request = main_module.DashboardCommitPushRequest(
        action="commit_push",
        approved=True,
        repository="owner/api",
        pbi_number=40,
        run_id="run-40",
    )
    with pytest.raises(StoreError, match="Agent worker is not configured"):
        main_module._execute_dashboard_action(SimpleNamespace(), "project-1", request)


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
        assert (run.last_result or "").startswith(
            "demo result\nGit delivery: no_changes"
        )
        assert run.lease_token is None
        state = client.get("/projects/project-1/dashboard").json()
        pbi = state["repositories"][0]["pbis"][0]
        assert pbi["status"] == "completed"
        assert pbi["result"] == run.last_result
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


def test_dashboard_start_uses_configured_task_and_keeps_display_label() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")

    class ConfiguredWorker:
        executor = SimpleNamespace(task="custom task")
        task: str | None = None

        def claim(self, project_id, repository, owner_id, task):
            self.task = task
            return service.claim(project_id, repository, owner_id)

        def start(self, run) -> None:
            del run

    worker = ConfiguredWorker()
    try:
        result = main_module._execute_dashboard_action(
            service,
            "project-1",
            main_module.DashboardStartRequest(
                action="start", approved=True, repository="owner/api"
            ),
            worker,
        )
        assert worker.task == "custom task"
        assert result["worker"]["task"] == main_module.DEMO_TASK_NAME
    finally:
        service.store.close()
