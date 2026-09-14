from collections.abc import Iterator

import pytest

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.routing import RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
)


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()


def seed_meta_review(store: OrchestratorStore, count: int = 1) -> None:
    store.sync_project(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    tuple(
                        PbiSnapshot("owner/api", number, f"PBI {number}")
                        for number in range(1, count + 1)
                    ),
                ),
            ),
        )
    )


def complete_meta_review(
    store: OrchestratorStore,
    pbi_number: int = 1,
    result: str = "completed result",
    *,
    project_id: str = "project-1",
    repository: str = "owner/api",
) -> str:
    run = store.claim_next(project_id, repository, f"worker-{pbi_number}")
    assert run is not None
    lease_token = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    contract = TaskContract.inventory(repository, pbi_number, f"PBI {pbi_number}")
    store.ensure_task_contract(run.run_id, contract, implementation.lease_token or "")
    store.record_task_result(
        run.run_id,
        TaskResult(TaskOutcome.PASS, {}),
        implementation.lease_token or "",
    )
    completed = store.complete_agent_run(run.run_id, result, lease_token)
    assert completed.status.value == "completed"
    return completed.run_id
