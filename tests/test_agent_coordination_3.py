import pytest

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import (
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.conftest import FakeProvider
from tests.support.agent.helpers import agent_snapshot as agent_snapshot


def test_agent_run_completion_and_failure_persist_bounded_state() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    try:
        with pytest.raises(StoreError, match="result is required"):
            store.complete_agent_run(run.run_id, " ", lease_token)
        with pytest.raises(StoreError, match="Unknown run"):
            store.complete_agent_run("missing", "result", lease_token)
        with pytest.raises(StoreError, match="active implementation"):
            store.complete_agent_run(run.run_id, "result", lease_token)

        implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
        contract = TaskContract.inventory("owner/api", run.pbi_number, run.title)
        store.ensure_task_contract(
            run.run_id, contract, implementation.lease_token or ""
        )
        store.record_task_result(
            run.run_id,
            TaskResult(TaskOutcome.PASS, {}),
            implementation.lease_token or "",
        )
        completed = store.complete_agent_run(
            run.run_id,
            "x" * 5_000,
            implementation.lease_token or "",
        )
        assert completed.status is RunStatus.COMPLETED
        assert completed.last_result == "x" * 4_000
        assert completed.lease_token is None
        assert store.complete_agent_run(run.run_id, "ignored", "stale") == completed
        with pytest.raises(StoreError, match="completed run"):
            store.fail_agent_run(run.run_id, "late failure", "stale")
    finally:
        store.close()

    failure_store = OrchestratorStore()
    failure_service = Orchestrator(failure_store, FakeProvider(agent_snapshot()))
    failure_service.synchronize("project-1")
    failed_run = failure_service.claim("project-1", "owner/api", "worker-1")
    assert failed_run is not None
    failure_lease = failed_run.lease_token or ""
    try:
        with pytest.raises(StoreError, match="failure reason"):
            failure_store.fail_agent_run(failed_run.run_id, " ", failure_lease)
        with pytest.raises(StoreError, match="Unknown run"):
            failure_store.fail_agent_run("missing", "error", failure_lease)
        failed = failure_store.fail_agent_run(
            failed_run.run_id,
            "e" * 20_000,
            failure_lease,
        )
        assert failed.status is RunStatus.FAILED
        assert failed.last_error == "e" * 16_000
        assert failed.lease_token is None
        assert (
            failure_store.fail_agent_run(failed_run.run_id, "ignored", "stale")
            == failed
        )
        state = failure_store.project_state("project-1")
        pbi = state["repositories"][0]["pbis"][0]
        assert pbi["claimable"] is True
    finally:
        failure_store.close()


def test_lease_loss_failure_recovery_clears_active_worker_state() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease)
    try:
        with pytest.raises(StoreError, match="Unknown run"):
            store.set_run_claimable("missing", False)
        with pytest.raises(StoreError, match="failure reason"):
            store.fail_agent_run_after_lease_loss(
                run.run_id, " ", expected_lease_token=lease
            )
        with pytest.raises(StoreError, match="lease token"):
            store.fail_agent_run_after_lease_loss(
                run.run_id, "worker failed", expected_lease_token=""
            )
        with pytest.raises(StoreError, match="Unknown run"):
            store.fail_agent_run_after_lease_loss(
                "missing", "worker failed", expected_lease_token=lease
            )
        failed = store.fail_agent_run_after_lease_loss(
            implementation.run_id,
            "lease expired while executing",
            expected_lease_token=lease,
        )
        assert failed.status is RunStatus.FAILED
        assert failed.lease_token is None
        assert (
            store.fail_agent_run_after_lease_loss(
                run.run_id, "ignored", expected_lease_token=lease
            )
            == failed
        )
    finally:
        store.close()
