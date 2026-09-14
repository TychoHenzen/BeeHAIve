from beehaiive.contracts import (
    TaskContract,
    TaskOutcome,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.storage import (
    OrchestratorStore,
)


def test_sync_does_not_reopen_a_contract_without_a_result(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "missing-result.sqlite3")
    project = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (PbiSnapshot("owner/api", 1, "PBI", stage=Stage.IMPLEMENT),),
            ),
        ),
    )
    store.sync_project(project)
    run = store.claim_next("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    contract = TaskContract(
        "test.contract", 1, "inspect", {}, (), (), tuple(TaskOutcome)
    )
    store.ensure_task_contract(run.run_id, contract, lease)
    store.fail_agent_run(run.run_id, "worker stopped", lease, claimable=True)

    store._connection.execute(
        "UPDATE pbis SET claimable = 1 WHERE project_id = ? "
        "AND repository_name = ? AND number = ?",
        ("project-1", "owner/api", 1),
    )
    store.sync_project(project)

    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    assert store.claim_next("project-1", "owner/api", "worker-2") is None
    store.close()
