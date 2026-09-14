from pathlib import Path

from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    RoutingStatus,
    RoutingStore,
)
from tests.support.routing.helpers import build_routing_config

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_bounce_limit_and_long_context_are_recorded() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_rounds=10, max_bounces=1))
    router.begin("problem-1")
    result = router.record(
        "problem-1",
        AttemptOutcome.FAILURE,
        failure_context="x" * 400,
    )

    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert "bounces" in str(result.state.required_action)
    assert result.attempt is not None
    assert result.attempt.failure_context is None
    assert len(result.decision.failure_context or "") == 280
    store.close()
