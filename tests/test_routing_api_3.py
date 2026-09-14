from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_run_attempt_rejects_a_lost_execution_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    def reject_validation(*args: object, **kwargs: object) -> None:
        raise StoreError("execution claim lost")

    monkeypatch.setattr(orchestrator_store, "validate_execution", reject_validation)
    with pytest.raises(StoreError, match="execution claim lost"):
        service.run_implementation_attempt(run.run_id, token)

    routing_store.close()
    orchestrator_store.close()


def test_routing_api_requires_the_run_lease_scope() -> None:
    routing_store = RoutingStore()
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            routing_store=routing_store,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    worker_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}
    client.post("/projects/owner:7/sync", headers=worker_headers)
    claim = client.post(
        "/projects/owner:7/repositories/owner/api/claim", headers=worker_headers
    )
    run_id = claim.json()["run_id"]
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim.json()["lease_token"],
    }
    client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )

    missing_lease = client.get(
        f"/runs/{run_id}/routing", headers={"X-API-Key": "test-key"}
    )
    wrong_lease = client.get(
        f"/runs/{run_id}/routing",
        headers={"X-API-Key": "test-key", "X-Lease-Token": "wrong"},
    )
    scoped = client.get(f"/runs/{run_id}/routing", headers=lease_headers)

    assert missing_lease.status_code == 401
    assert wrong_lease.status_code == 403
    assert scoped.status_code == 200
    assert scoped.json()["state"]["problem_id"] == run_id

    routing_store.close()
    orchestrator_store.close()


def test_routing_api_maps_duplicate_unknown_and_invalid_requests() -> None:
    routing_store = RoutingStore()
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider())
    client = TestClient(
        create_app(
            orchestrator=service,
            routing_store=routing_store,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}
    client.post("/projects/owner:7/sync", headers=headers)
    claim = client.post(
        "/projects/owner:7/repositories/owner/api/claim", headers=headers
    )
    run_id = claim.json()["run_id"]
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim.json()["lease_token"],
    }
    client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    duplicate = client.post(
        "/routing/problems", json={"problem_id": "duplicate"}, headers=headers
    )
    missing = client.get(
        "/runs/missing/routing",
        headers={"X-API-Key": "test-key", "X-Lease-Token": "missing"},
    )
    invalid_retry = client.post(
        f"/runs/{run_id}/routing/attempts",
        json={"outcome": "retry", "failure_context": "not allowed"},
        headers=lease_headers,
    )

    assert duplicate.status_code == 404
    assert missing.status_code == 403
    assert invalid_retry.status_code == 409
    routing_store.close()
    orchestrator_store.close()
