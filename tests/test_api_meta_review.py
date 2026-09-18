from fastapi.testclient import TestClient

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def test_accept_meta_review_uses_shared_pbi_creation_service() -> None:
    store = OrchestratorStore()
    routing = RoutingStore()
    provider = ApiProvider()
    store.sync_project(
        ProjectSnapshot(
            "owner:7",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot("owner/api", 1, "PBI"),
                        PbiSnapshot("owner/api", 2, "PBI 2"),
                    ),
                ),
            ),
        )
    )
    run_ids: list[str] = []
    for pbi_number, title in ((1, "PBI"), (2, "PBI 2")):
        run = store.claim_next("owner:7", "owner/api", f"worker-{pbi_number}")
        assert run is not None
        lease_token = run.lease_token or ""
        implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
        store.ensure_task_contract(
            run.run_id,
            TaskContract.inventory("owner/api", pbi_number, title),
            implementation.lease_token or "",
        )
        store.record_task_result(
            run.run_id,
            TaskResult(TaskOutcome.PASS, {}),
            implementation.lease_token or "",
        )
        store.complete_agent_run(run.run_id, "Completed the PBI.", lease_token)
        run_ids.append(run.run_id)
    router = ModelRouter(routing)
    for run_id in run_ids:
        router.begin(run_id)
        router.record(run_id, "failure", failure_context="fixture failure")
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
