from pathlib import Path

import pytest

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    ModelTier,
    RoutingAttempt,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_later_failure_replays_only_its_own_pending_routing_transition(
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
    service.fail(run.run_id, "first failure", token, input_tokens=2, output_tokens=1)

    retried = service.claim("owner:7", "owner/api", "worker-1")
    assert retried is not None
    retry_token = retried.lease_token or ""
    original_record = router.record
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RoutingError("temporary routing storage failure")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(router, "record", fail_once)
    with pytest.raises(StoreError, match="temporary routing storage failure"):
        service.fail(
            retried.run_id,
            "second failure",
            retry_token,
            input_tokens=4,
            output_tokens=2,
        )

    service.fail(
        retried.run_id,
        "second failure repeated",
        retry_token,
        input_tokens=100,
        output_tokens=100,
    )

    routed = router.snapshot(run.run_id)
    assert calls == 2
    assert len(routed.attempts) == 2
    assert routed.attempts[-1].input_tokens == 4
    assert routed.attempts[-1].output_tokens == 2
    assert routed.state.consecutive_failures == 2
    assert routed.state.current_tier is ModelTier.SOL

    routing_store.close()
    orchestrator_store.close()


def test_orchestrator_surfaces_routing_transition_error() -> None:
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

    with pytest.raises(StoreError, match="already resolved"):
        service.fail(run.run_id, "a later failure", token)

    routing_store.close()
    orchestrator_store.close()


def test_routing_rejects_invalid_transitions_and_persists_transaction_failures() -> (
    None
):
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())
    started = router.begin("problem-1")

    with pytest.raises(RoutingError, match="already exists"):
        router.begin("problem-1")
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        router.snapshot("missing")
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        router.handoff_limit_reason("missing")
    with pytest.raises(RoutingError, match="Unknown attempt outcome"):
        router.record("problem-1", "unknown")
    with pytest.raises(RoutingError, match="Unknown routing transition outcome"):
        router.record(
            "problem-1",
            AttemptOutcome.SUCCESS,
            transition_outcome="unknown",
        )
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        router.record("missing", AttemptOutcome.SUCCESS)
    with pytest.raises(RoutingError, match="must not be negative"):
        router.record("problem-1", AttemptOutcome.SUCCESS, input_tokens=-1)
    with pytest.raises(RoutingError, match="depth"):
        router.record("problem-1", AttemptOutcome.SUCCESS, recursive_spawn_depth=-1)
    with pytest.raises(RoutingError, match="reason"):
        router.record("problem-1", AttemptOutcome.SUCCESS, force_human_reason=" ")
    with pytest.raises(RoutingError, match="Failure context"):
        router.record("problem-1", AttemptOutcome.FAILURE)
    with pytest.raises(RoutingError, match="Only a triage"):
        router.record(
            "problem-1", AttemptOutcome.RETRY, failure_context="invalid writer retry"
        )

    attempt = RoutingAttempt(
        attempt_id=None,
        problem_id="problem-1",
        round=1,
        model="cheap-writer",
        tier=ModelTier.LUNA,
        reason="routine",
        outcome=AttemptOutcome.SUCCESS,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        estimated_cost=0.0,
        bounce_count=0,
        recursive_spawn_depth=0,
        failure_context=None,
        created_at="now",
    )
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        store.save_transition("missing", 0, started.state, attempt)
    with pytest.raises(RoutingError, match="changed"):
        store.save_transition("problem-1", 1, started.state, attempt)
    with pytest.raises(RoutingError, match="already"):
        store.create_problem(started.state)

    resolved = router.record("problem-1", AttemptOutcome.SUCCESS)
    with pytest.raises(RoutingError, match="already resolved"):
        router.record("problem-1", AttemptOutcome.SUCCESS)
    with pytest.raises(RoutingError, match="already resolved"):
        router.execute("problem-1", FakeRoutingModel())
    assert resolved.state.status is RoutingStatus.RESOLVED

    with pytest.raises(RoutingError, match="required"):
        router.begin(" ")
    with pytest.raises(RoutingError, match="200 characters"):
        router.begin("x" * 201)
    store.close()
