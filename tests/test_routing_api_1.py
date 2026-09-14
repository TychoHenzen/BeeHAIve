from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    ModelRouter,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_routing_api_returns_persisted_attempt_accounting() -> None:
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
    started = client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    failed = client.post(
        f"/runs/{run_id}/routing/attempts",
        json={
            "outcome": "failure",
            "input_tokens": 20,
            "output_tokens": 5,
            "failure_context": "The API attempt failed.",
        },
        headers=lease_headers,
    )
    fetched = client.get(f"/runs/{run_id}/routing", headers=lease_headers)

    assert started.status_code == 200
    assert started.json()["routing"]["decision"]["tier"] == "luna"
    assert failed.status_code == 200
    assert failed.json()["decision"]["tier"] == "terra"
    assert failed.json()["attempt"]["model"] == "luna"
    assert failed.json()["attempt"]["tier"] == "luna"
    assert failed.json()["attempt"]["reason"] == "routine"
    assert failed.json()["attempt"]["input_tokens"] == 20
    assert failed.json()["attempt"]["output_tokens"] == 5
    assert failed.json()["attempt"]["total_tokens"] == 25
    assert failed.json()["attempt"]["estimated_cost"] > 0
    assert failed.json()["attempt"]["bounce_count"] == 1
    assert failed.json()["attempt"]["recursive_spawn_depth"] == 0
    assert fetched.status_code == 200
    assert fetched.json()["state"]["consecutive_failures"] == 1
    assert fetched.json()["attempts"][0]["tier"] == "luna"
    assert fetched.json()["attempts"][0]["bounce_count"] == 1

    routing_store.close()
    orchestrator_store.close()


def test_create_app_shares_or_rejects_conflicting_routing_configuration() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    router.begin("shared-problem")
    assert router.snapshot("shared-problem").state.problem_id == "shared-problem"

    conflicting_router_store = RoutingStore()
    with pytest.raises(ValueError, match="share one model router"):
        create_app(
            orchestrator=service,
            model_router=ModelRouter(conflicting_router_store, build_routing_config()),
        )
    with pytest.raises(ValueError, match="share one routing store"):
        create_app(orchestrator=service, routing_store=conflicting_router_store)

    standalone_store = RoutingStore()
    standalone_router_store = RoutingStore()
    with pytest.raises(ValueError, match="share one routing store"):
        create_app(
            routing_store=standalone_store,
            model_router=ModelRouter(standalone_router_store, build_routing_config()),
        )

    conflicting_router_store.close()
    standalone_store.close()
    standalone_router_store.close()
    routing_store.close()
    orchestrator_store.close()


def test_run_failure_api_passes_usage_to_connected_routing() -> None:
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
    failed = client.post(
        f"/runs/{run_id}/fail",
        json={
            "error": "verification failed",
            "input_tokens": 12,
            "output_tokens": 4,
            "recursive_spawn_depth": 1,
        },
        headers=lease_headers,
    )
    routed = client.get(f"/runs/{run_id}/routing", headers=lease_headers)

    assert failed.status_code == 200
    assert failed.json()["routing"]["decision"]["tier"] == "terra"
    assert failed.json()["routing"]["decision"]["requires_human"] is False
    assert routed.status_code == 200
    attempt = routed.json()["attempts"][0]
    assert routed.json()["decision"]["tier"] == "terra"
    assert attempt["model"] == "luna"
    assert attempt["input_tokens"] == 12
    assert attempt["output_tokens"] == 4
    assert attempt["total_tokens"] == 16
    assert attempt["estimated_cost"] > 0
    assert attempt["recursive_spawn_depth"] == 1
    assert attempt["bounce_count"] == 1

    routing_store.close()
    orchestrator_store.close()


def test_implement_advance_api_exposes_the_initial_routing_decision() -> None:
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
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim.json()["lease_token"],
    }

    advanced = client.post(
        f"/runs/{claim.json()['run_id']}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )

    assert advanced.status_code == 200
    assert advanced.json()["routing"]["decision"]["tier"] == "luna"
    assert advanced.json()["routing"]["decision"]["requires_human"] is False

    routing_store.close()
    orchestrator_store.close()
