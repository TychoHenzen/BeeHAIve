from dataclasses import replace
from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.models import (
    RunState,
    RunStatus,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


@pytest.mark.parametrize(
    "failure_point",
    [
        "workspace_cleanup",
        "worker_start",
        "expired_lease_race",
        "rotated_lease_race",
    ],
)
def test_worker_manager_handles_failed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    executor.task = "persisted recovery task"
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class IdleThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            return None

    monkeypatch.setattr(
        "beehaiive.agent_parts.worker_capacity_mixin.Thread", IdleThread
    )
    original_manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    original_manager.start(run)
    lease_token = run.lease_token
    assert lease_token is not None
    store.record_agent_session_event(
        run.run_id, lease_token, "progress", "turn.started", None, "saved progress"
    )
    original_session = store.get_agent_session(run.run_id)
    assert original_session is not None
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )

    recovery_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    recovery_executor.task = ""
    recovery_manager = AgentWorkerManager(
        orchestrator, recovery_executor, workflow_service
    )
    try:
        with monkeypatch.context() as context:
            if failure_point != "worker_start":

                def fail_cleanup(_run_id: str) -> None:
                    raise RuntimeError("workspace cleanup failed")

                context.setattr(
                    workflow_service, "cleanup_dashboard_run_workspaces", fail_cleanup
                )
            else:

                class FailingThread:
                    def __init__(self, target, args, name, daemon) -> None:
                        del target, args, name, daemon

                    def start(self) -> None:
                        raise RuntimeError("thread start failed")

                context.setattr(
                    "beehaiive.agent_parts.worker_capacity_mixin.Thread",
                    FailingThread,
                )

            if failure_point in {"expired_lease_race", "rotated_lease_race"}:
                fail_agent_run = store.fail_agent_run

                def fail_after_lease_change(
                    run_id: str,
                    error: str,
                    claimed_token: str,
                    *,
                    claimable: bool | None = None,
                ) -> RunState:
                    if failure_point == "expired_lease_race":
                        store._connection.execute(
                            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
                            ("2000-01-01T00:00:00+00:00", run_id),
                        )
                    else:
                        store._connection.execute(
                            """
                            UPDATE runs
                            SET owner_id = ?, lease_token = ?, lease_expires_at = ?
                            WHERE run_id = ?
                            """,
                            (
                                "replacement-worker",
                                "replacement-lease-token",
                                "2099-01-01T00:00:00+00:00",
                                run_id,
                            ),
                        )
                    return fail_agent_run(
                        run_id, error, claimed_token, claimable=claimable
                    )

                context.setattr(store, "fail_agent_run", fail_after_lease_change)
            assert recovery_manager.recover() == ()

        result_run = store.get_run(run.run_id)
        assert result_run is not None
        if failure_point == "rotated_lease_race":
            assert result_run.status is RunStatus.ACTIVE
            assert result_run.lease_token == "replacement-lease-token"
            assert result_run.owner_id == "replacement-worker"
        else:
            assert result_run.status is RunStatus.FAILED
            assert result_run.lease_token is None
            assert "Agent recovery failed" in (result_run.last_error or "")
        assert run.run_id not in recovery_manager._threads
        assert run.run_id not in recovery_manager._workspace_leases
        failed_session = store.get_agent_session(run.run_id)
        assert failed_session is not None
        assert failed_session["session_id"] == original_session["session_id"]
        assert failed_session["task"] == original_session["task"]
        assert failed_session["events"] == original_session["events"]
        assert failed_session["worker_id"] == recovery_manager._worker_id
        assert recovery_manager.recover() == ()
    finally:
        workflow_service.cleanup_dashboard_run_workspaces()
        executor.release_run(run.run_id)
        recovery_executor.release_run(run.run_id)
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_recovery_rejects_claim_without_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    assert run.lease_token is not None
    store.start_agent_session(run.run_id, "worker-1", "persisted task", run.lease_token)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    monkeypatch.setattr(
        manager,
        "claim",
        lambda *_args, **_kwargs: replace(run, lease_token=None),
    )

    try:
        with pytest.raises(StoreError, match="no lease token"):
            manager.recover()
    finally:
        workflow_store.close()
        store.close()
        routing_store.close()
