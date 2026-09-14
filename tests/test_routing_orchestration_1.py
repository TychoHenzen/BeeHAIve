import time
from concurrent.futures import ThreadPoolExecutor
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
    ModelTier,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app
from tests.support.routing.blocking_routing_model import (
    BlockingRoutingModel as BlockingRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_routing_snapshot_errors_are_returned_by_run_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)
    client = TestClient(
        create_app(
            orchestrator=service,
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
    original_snapshot = router.snapshot

    def reject_snapshot(*args: object, **kwargs: object) -> object:
        raise RoutingError("routing snapshot unavailable")

    monkeypatch.setattr(router, "snapshot", reject_snapshot)
    failed_advance = client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    assert failed_advance.status_code == 409
    assert "routing snapshot unavailable" in failed_advance.json()["detail"]

    monkeypatch.setattr(router, "snapshot", original_snapshot)
    advanced = client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    assert advanced.status_code == 200

    monkeypatch.setattr(router, "snapshot", reject_snapshot)
    failed = client.post(
        f"/runs/{run_id}/fail",
        json={"error": "implementation failed"},
        headers=lease_headers,
    )
    assert failed.status_code == 409
    assert "routing snapshot unavailable" in failed.json()["detail"]

    routing_store.close()
    orchestrator_store.close()


def test_orchestrator_rejects_an_invalid_executor_contract() -> None:
    routing_store = RoutingStore()
    store = OrchestratorStore()

    class InvalidContractExecutor:
        def build_task_contract(self, _run: object) -> object:
            return object()

    service = Orchestrator(
        store,
        RoutingProvider(),
        ModelRouter(routing_store),
        InvalidContractExecutor(),  # type: ignore[arg-type]
    )
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    service.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "")
    with pytest.raises(StoreError, match="invalid task contract"):
        service.run_implementation_attempt(run.run_id, run.lease_token or "")

    routing_store.close()
    store.close()


def test_orchestrator_connects_implementation_failures_to_model_router() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    advanced = service.advance(run.run_id, Stage.IMPLEMENT, token)
    started = router.snapshot(run.run_id)
    failed = service.fail(run.run_id, "implementation verification failed", token)
    routed = router.snapshot(run.run_id)

    assert advanced.stage is Stage.IMPLEMENT
    assert started.decision.tier is ModelTier.LUNA
    assert failed.status.value == "failed"
    assert routed.decision.tier is ModelTier.TERRA
    assert routed.state.consecutive_failures == 1
    assert routed.attempts[0].model == "cheap-writer"
    assert routed.attempts[0].failure_context is None
    assert routed.attempts[0].reason == "routine"

    routing_store.close()
    orchestrator_store.close()


def test_successful_handoff_resets_the_connected_routing_problem() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    service.fail(run.run_id, "implementation failed", token)
    retried = service.claim("owner:7", "owner/api", "worker-1")
    assert retried is not None
    retry_token = retried.lease_token or ""

    completed = service.handoff(
        retried.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        retry_token,
    )
    routed = router.snapshot(run.run_id)

    assert completed.status.value == "completed"
    assert routed.state.status is RoutingStatus.RESOLVED
    assert routed.state.consecutive_failures == 0
    assert routed.state.bounce_count == 0
    assert [attempt.outcome for attempt in routed.attempts] == [
        AttemptOutcome.FAILURE,
        AttemptOutcome.SUCCESS,
    ]

    routing_store.close()
    orchestrator_store.close()


def test_human_handoff_stays_nonclaimable_until_explicit_reset() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(
        routing_store, build_routing_config(max_rounds=1, max_bounces=10)
    )
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    failed = service.fail(run.run_id, "implementation failed", token)
    handoff = router.snapshot(run.run_id)
    assert handoff.state.status is RoutingStatus.HUMAN_HANDOFF
    assert failed.status.value == "awaiting_operator"

    waiting = orchestrator_store.get_run(run.run_id)
    question = orchestrator_store.operator_question_for_run(run.run_id)
    assert waiting is not None
    assert waiting.status.value == "awaiting_operator"
    assert question is not None
    assert question["kind"] == "routing_exhausted"
    assert question["status"] == "pending"

    assert service.claim("owner:7", "owner/api", "worker-1") is None
    pbi = orchestrator_store.project_state("owner:7")["repositories"][0]["pbis"][0]
    assert pbi["claimable"] is False
    assert handoff.state.required_action

    routing_store.close()
    orchestrator_store.close()


def test_long_model_execution_renews_shared_run_lease(tmp_path: Path) -> None:
    state_database = tmp_path / "state.sqlite3"
    routing_database = tmp_path / "routing.sqlite3"
    first_store = OrchestratorStore(state_database, lease_seconds=1)
    first_router = ModelRouter(RoutingStore(routing_database), build_routing_config())
    model = BlockingRoutingModel()
    first_service = Orchestrator(first_store, RoutingProvider(), first_router, model)
    first_service.synchronize("owner:7")
    run = first_service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    first_service.advance(run.run_id, Stage.IMPLEMENT, token)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            first_service.run_implementation_attempt, run.run_id, token
        )
        assert model.started.wait(timeout=2)
        second_store = OrchestratorStore(state_database, lease_seconds=1)
        second_service = Orchestrator(
            second_store,
            RoutingProvider(),
            ModelRouter(RoutingStore(routing_database), build_routing_config()),
            model,
        )
        time.sleep(1.2)
        assert second_service.claim("owner:7", "owner/api", "worker-2") is None
        model.release.set()
        result = future.result()

    assert len(result.attempts) == 1
    second_store.close()
    first_router.store.close()
    first_store.close()
