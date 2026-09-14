import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.routing.blocking_routing_model import (
    BlockingRoutingModel as BlockingRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_reclaimed_run_rejects_stale_model_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def reject_heartbeat(*args: object, **kwargs: object) -> None:
        raise StoreError("heartbeat stopped")

    monkeypatch.setattr(first_store, "heartbeat_execution", reject_heartbeat)
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
        reclaimed = second_service.claim("owner:7", "owner/api", "worker-2")
        assert reclaimed is not None
        model.release.set()
        with pytest.raises(StoreError, match="heartbeat|claim"):
            future.result()

    assert first_router.snapshot(run.run_id).attempts == ()
    second_store.close()
    first_router.store.close()
    first_store.close()


def test_handoff_does_not_complete_when_success_hits_a_routing_limit() -> None:
    class CountingProvider(RoutingProvider):
        def __init__(self) -> None:
            self.handoff_calls = 0

        def create_handoff(self, request: HandoffRequest) -> HandoffResult:
            self.handoff_calls += 1
            return super().create_handoff(request)

    routing_store = RoutingStore()
    router = ModelRouter(
        routing_store, build_routing_config(max_rounds=1, max_bounces=10)
    )
    orchestrator_store = OrchestratorStore()
    provider = CountingProvider()
    service = Orchestrator(orchestrator_store, provider, router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    with pytest.raises(StoreError, match="requires human action"):
        service.handoff(run.run_id, "codex/api-1", "master", "Closes #1", token)

    assert provider.handoff_calls == 0
    assert router.snapshot(run.run_id).state.status is RoutingStatus.ACTIVE
    assert orchestrator_store.pending_handoff(run.run_id, token) is not None

    routing_store.close()
    orchestrator_store.close()


def test_repeating_a_failed_run_does_not_duplicate_routing_attempts() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    service.fail(
        run.run_id,
        "implementation failed",
        token,
        input_tokens=10,
        output_tokens=5,
    )
    service.fail(
        run.run_id,
        "same failure repeated",
        token,
        input_tokens=100,
        output_tokens=100,
    )

    routed = router.snapshot(run.run_id)
    assert len(routed.attempts) == 1
    assert routed.state.total_tokens == 15
    assert routed.attempts[0].failure_context is None

    routing_store.close()
    orchestrator_store.close()


def test_failed_run_treats_post_commit_routing_error_as_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    original_record = router.record

    def record_then_report_error(*args: object, **kwargs: object) -> object:
        original_record(*args, **kwargs)
        raise RoutingError("routing response lost after commit")

    monkeypatch.setattr(router, "record", record_then_report_error)
    failed = service.fail(run.run_id, "implementation failed", token)

    assert failed.status.value == "failed"
    assert len(router.snapshot(run.run_id).attempts) == 1

    routing_store.close()
    orchestrator_store.close()


def test_concurrent_failed_run_requests_record_one_routing_attempt() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.fail,
                run.run_id,
                "concurrent failure",
                token,
                input_tokens=10,
                output_tokens=5,
            ),
            executor.submit(
                service.fail,
                run.run_id,
                "concurrent failure",
                token,
                input_tokens=100,
                output_tokens=100,
            ),
        ]
        failed_runs = [future.result() for future in futures]

    routed = router.snapshot(run.run_id)
    assert all(failed.status.value == "failed" for failed in failed_runs)
    assert len(routed.attempts) == 1
    assert routed.state.total_tokens in {15, 200}

    routing_store.close()
    orchestrator_store.close()


def test_handoff_skips_already_resolved_routing_state() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    router.record(run.run_id, AttemptOutcome.SUCCESS)

    completed = service.handoff(
        run.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        token,
    )

    assert completed.status.value == "completed"
    routing_store.close()
    orchestrator_store.close()
