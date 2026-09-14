from pathlib import Path

import pytest

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    ModelRouter,
    ModelTier,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.over_budget_routing_model import (
    OverBudgetRoutingModel as OverBudgetRoutingModel,
)
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_handoff_surfaces_routing_success_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())

    def reject_success(*args: object, **kwargs: object) -> object:
        raise RoutingError("routing success rejected")

    monkeypatch.setattr(router, "record", reject_success)
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    with pytest.raises(StoreError, match="routing success rejected"):
        service.handoff(
            run.run_id,
            "codex/api-1",
            "master",
            "Closes #1",
            token,
        )

    pending_run = orchestrator_store.get_run(run.run_id)
    assert pending_run is not None
    assert pending_run.status.value == "active"
    pending_intent = orchestrator_store.pending_handoff(run.run_id, token)
    assert pending_intent is not None
    assert pending_intent.branch == "codex/api-1"

    routing_store.close()
    orchestrator_store.close()


def test_model_double_stops_after_human_handoff() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_rounds=3, max_bounces=10))
    model = FakeRoutingModel()
    decision = router.begin("model-loop").decision

    while not decision.requires_human:
        decision = router.execute("model-loop", model).decision

    snapshot = router.snapshot("model-loop")
    assert decision.tier is ModelTier.HUMAN
    assert model.calls == 3
    assert model.calls == len(snapshot.attempts)
    assert snapshot.state.required_action is not None
    with pytest.raises(RoutingError, match="already human_handoff"):
        router.execute("model-loop", model)
    assert model.calls == 3
    store.close()


def test_model_executor_receives_budgets_and_overuse_stops_at_human_handoff() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_tokens=5))
    model = OverBudgetRoutingModel()
    router.begin("over-budget")

    result = router.execute("over-budget", model)

    assert model.calls == 1
    assert model.budgets == [(5, 8, 2)]
    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert result.state.total_tokens == 5
    assert result.attempt is not None
    assert result.attempt.total_tokens == 5
    assert "Token ceiling reached" in str(result.state.required_action)
    store.close()


def test_orchestrator_surfaces_routing_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())

    def reject_start(problem_id: str) -> object:
        raise RoutingError("routing start rejected")

    monkeypatch.setattr(router, "begin", reject_start)
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""

    with pytest.raises(StoreError, match="routing start rejected"):
        service.advance(run.run_id, Stage.IMPLEMENT, token)

    routing_store.close()
    orchestrator_store.close()
