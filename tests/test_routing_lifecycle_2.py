import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    ModelRouter,
    RoutingError,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider
from tests.support.routing.unannotated_failing_routing_model import (
    UnannotatedFailingRoutingModel as UnannotatedFailingRoutingModel,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_routing_snapshot_endpoint_maps_errors_and_rejects_missing_path_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    orchestrator_store = OrchestratorStore()
    router = ModelRouter(routing_store, build_routing_config())
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)
    app = create_app(
        orchestrator=service,
        api_key="test-key",
        allowed_project_ids={"owner:7"},
    )
    client = TestClient(app)
    worker_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}
    client.post("/projects/owner:7/sync", headers=worker_headers)
    claim = client.post(
        "/projects/owner:7/repositories/owner/api/claim", headers=worker_headers
    )
    run_id = claim.json()["run_id"]
    lease_token = claim.json()["lease_token"]
    lease_headers = {"X-API-Key": "test-key", "X-Lease-Token": lease_token}
    client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )

    route = next(
        route for route in app.routes if route.path == "/runs/{run_id}/routing"
    )
    dependency = route.dependant.dependencies[0].call
    assert dependency is not None
    with pytest.raises(HTTPException) as missing_path:
        dependency(
            Request({"type": "http", "path_params": {}}), "test-key", lease_token
        )
    assert missing_path.value.status_code == 403

    def reject_snapshot(*args: object, **kwargs: object) -> object:
        raise RoutingError("routing snapshot unavailable")

    monkeypatch.setattr(router, "snapshot", reject_snapshot)
    response = client.get(f"/runs/{run_id}/routing", headers=lease_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "routing snapshot unavailable"

    routing_store.close()
    orchestrator_store.close()


def test_execution_storage_guards_reject_unknown_and_duplicate_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, RoutingProvider())

    with pytest.raises(StoreError, match="Unknown run"):
        store.validate_lease("missing", "missing")
    with pytest.raises(StoreError, match="Unknown run"):
        store.claim_execution("missing", "missing")
    with pytest.raises(StoreError, match="Unknown run"):
        store.validate_execution("missing", "missing", "missing")
    with pytest.raises(StoreError, match="Unknown run"):
        store.heartbeat_execution("missing", "missing", "missing")

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    with pytest.raises(StoreError, match="implementation run"):
        store.claim_execution(run.run_id, token)
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    execution_token = store.claim_execution(run.run_id, token)
    with pytest.raises(StoreError, match="already active"):
        store.claim_execution(run.run_id, token)
    with pytest.raises(StoreError, match="no longer valid"):
        store.validate_execution(run.run_id, token, "wrong")
    with pytest.raises(StoreError, match="no longer valid"):
        store.heartbeat_execution(run.run_id, token, "wrong")
    store.release_execution(run.run_id, execution_token)

    with pytest.raises(StoreError, match="Token usage"):
        store.fail_with_transition(run.run_id, "failure", token, input_tokens=-1)
    with pytest.raises(StoreError, match="Recursive spawn"):
        store.fail_with_transition(
            run.run_id, "failure", token, recursive_spawn_depth=-1
        )

    original_run_for_id = store._run_for_id

    def delete_after_read(connection: sqlite3.Connection, run_id: str) -> object:
        row = original_run_for_id(connection, run_id)
        if row is not None:
            store._connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return row

    monkeypatch.setattr(store, "_run_for_id", delete_after_read)
    with pytest.raises(StoreError, match="Unknown run"):
        store.claim_execution(run.run_id, token)
    store.close()


def test_unannotated_model_exception_gets_a_bounded_failure_context() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())
    router.begin("unannotated-provider-failure")

    result = router.execute(
        "unannotated-provider-failure", UnannotatedFailingRoutingModel()
    )

    assert (
        result.state.last_failure_context
        == "Model execution failed: provider unavailable"
    )
    store.close()
