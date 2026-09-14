from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.agent.helpers import agent_snapshot as agent_snapshot
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_manager_recovers_expired_session_without_cleaning_other_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    first_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    first_executor.task = "configured task " + "x" * 5_000
    orchestrator, store, routing_store, run = service_with_run(first_executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class IdleThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(
        "beehaiive.agent_parts.worker_capacity_mixin.Thread", IdleThread
    )
    first_manager = AgentWorkerManager(orchestrator, first_executor, workflow_service)
    first_manager.start(run)
    original_session = store.get_agent_session(run.run_id)
    assert original_session is not None
    original_workspace = workflow_service.workspace_for_run(run.run_id)
    assert original_workspace is not None
    other_workspace = workflow_service.acquire_workspace(
        "dashboard-run:other", "codex/other", tmp_path / "other-worktree"
    )
    second_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    second_executor.task = ""
    second_manager = AgentWorkerManager(orchestrator, second_executor, workflow_service)
    try:
        with monkeypatch.context() as context:
            context.setattr(store, "get_agent_session", lambda _run_id: {"task": ""})
            assert second_manager.recover() == ()
        assert Path(original_workspace.worktree_path).exists()
        store._connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )
        assert second_manager.recover() == (run.run_id,)
        recovered_session = store.get_agent_session(run.run_id)
        assert recovered_session is not None
        assert recovered_session["session_id"] == original_session["session_id"]
        assert recovered_session["task"] == original_session["task"]
        assert len(str(recovered_session["task"])) > 4_000
        assert recovered_session["worker_id"] == second_manager._worker_id
        assert second_executor._session_tasks[run.run_id] == original_session["task"]
        assert not Path(original_workspace.worktree_path).exists()
        assert workflow_store.get_lease(other_workspace.lease_id) == other_workspace
    finally:
        workflow_service.cleanup_dashboard_run_workspaces()
        first_executor.release_run(run.run_id)
        second_executor.release_run(run.run_id)
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_claim_persists_session_before_start() -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused")
    )
    store = OrchestratorStore()
    routing_store = RoutingStore()
    orchestrator = Orchestrator(
        store,
        FakeProvider(agent_snapshot()),
        ModelRouter(routing_store),
        executor,
    )
    orchestrator.synchronize("project-1")
    manager = AgentWorkerManager(orchestrator, executor)

    try:
        with pytest.raises(StoreError, match="agent task"):
            manager.claim("project-1", "owner/api", "dashboard-operator", task=" ")
        assert store.active_runs_for_project("project-1") == ()
        task = "persisted task " + "x" * 5_000
        run = manager.claim("project-1", "owner/api", "dashboard-operator", task=task)
        assert run is not None
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["task"] == task
        assert session["worker_id"] == manager._worker_id
    finally:
        store.close()
        routing_store.close()
