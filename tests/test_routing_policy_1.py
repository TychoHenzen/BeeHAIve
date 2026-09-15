from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelSpec,
    ModelTier,
    RoutingConfig,
    RoutingError,
    RoutingLimits,
    RoutingStatus,
    RoutingStore,
)
from tests.support.routing.failing_routing_model import (
    FailingRoutingModel as FailingRoutingModel,
)
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.over_budget_routing_model import (
    OverBudgetRoutingModel as OverBudgetRoutingModel,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_success_resets_state_and_a_new_problem_starts_at_first_triage() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())

    router.begin("problem-1")
    router.record(
        "problem-1",
        AttemptOutcome.FAILURE,
        failure_context="The first attempt failed.",
    )
    router.record(
        "problem-1",
        AttemptOutcome.RETRY,
        failure_context="Use the smaller failing fixture.",
    )
    resolved = router.record("problem-1", AttemptOutcome.SUCCESS)

    assert resolved.state.status is RoutingStatus.RESOLVED
    assert resolved.state.consecutive_failures == 0
    assert resolved.state.bounce_count == 0
    assert resolved.state.last_failure_context is None

    next_problem = router.begin("problem-2")
    assert next_problem.decision.tier is ModelTier.LUNA
    next_failure = router.record(
        "problem-2",
        AttemptOutcome.FAILURE,
        failure_context="A new independent problem failed.",
    )
    assert next_failure.decision.tier is ModelTier.TERRA
    assert next_failure.state.consecutive_failures == 1

    store.close()


def test_model_override_is_used_and_recorded() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())
    model = FakeRoutingModel(outcomes=(AttemptOutcome.SUCCESS,))
    router.begin("override")

    result = router.execute("override", model, model_override="sol")

    assert model.models == ["sol"]
    assert result.attempt is not None and result.attempt.model == "sol"
    assert store.get_attempts("override")[0].model == "sol"
    store.close()


def test_triage_exhaustion_requires_human_action() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_rounds=10, max_bounces=10))
    router.begin("problem-1")

    for expected_next_tier in (ModelTier.TERRA, ModelTier.SOL, ModelTier.ASTRA):
        result = router.record(
            "problem-1",
            AttemptOutcome.FAILURE,
            failure_context="The current tier failed.",
        )
        assert result.decision.tier is expected_next_tier

    result = router.record(
        "problem-1",
        AttemptOutcome.FAILURE,
        failure_context="The final triage tier failed.",
    )

    assert result.decision.tier is ModelTier.HUMAN
    assert result.decision.requires_human is True
    assert result.state.required_action == (
        "Human action required: All configured triage tiers failed"
    )
    assert result.state.consecutive_failures == 4

    store.close()


def test_round_token_and_recursive_limits_require_human_action() -> None:
    cases = (
        ({"max_rounds": 1}, {}, "Maximum routing rounds reached"),
        ({"max_tokens": 5}, {"input_tokens": 6}, "Token ceiling reached"),
        (
            {"max_recursive_spawn_depth": 1},
            {"recursive_spawn_depth": 2},
            "Recursive spawn limit reached",
        ),
    )
    for limits, attempt_options, expected_reason in cases:
        store = RoutingStore()
        router = ModelRouter(store, build_routing_config(**limits))
        router.begin("problem-1")
        result = router.record(
            "problem-1",
            AttemptOutcome.FAILURE,
            failure_context="The bounded test failure.",
            **attempt_options,
        )

        assert result.state.status is RoutingStatus.HUMAN_HANDOFF
        assert result.decision.requires_human is True
        assert expected_reason in str(result.state.required_action)
        assert result.state.consecutive_failures == 1
        store.close()


@pytest.mark.parametrize(
    ("limits", "model_options", "expected_reason"),
    (
        ({"max_rounds": 1}, {}, "Maximum routing rounds reached"),
        (
            {"max_tokens": 5},
            {"input_tokens": 6, "output_tokens": 0},
            "Token ceiling reached",
        ),
        (
            {"max_recursive_spawn_depth": 1},
            {"recursive_spawn_depth": 2},
            "Recursive spawn limit reached",
        ),
        ({"max_bounces": 1}, {}, "Maximum routing bounces reached"),
    ),
)
def test_model_double_stops_at_each_configured_limit(
    limits: dict[str, int],
    model_options: dict[str, int],
    expected_reason: str,
) -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(**limits))
    model = FakeRoutingModel(**model_options)
    router.begin("bounded-model")

    result = router.snapshot("bounded-model")
    while result.state.status is RoutingStatus.ACTIVE:
        result = router.execute("bounded-model", model)

    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert expected_reason in str(result.state.required_action)
    assert model.calls == len(result.attempts)
    store.close()


@pytest.mark.parametrize(
    ("limits", "model_options", "expected_reason"),
    (
        ({"max_rounds": 1}, {}, "Maximum routing rounds reached"),
        (
            {"max_tokens": 1},
            {"input_tokens": 2, "output_tokens": 0},
            "Token ceiling reached",
        ),
        (
            {"max_recursive_spawn_depth": 0},
            {"recursive_spawn_depth": 1},
            "Recursive spawn limit reached",
        ),
    ),
)
def test_success_does_not_bypass_configured_limits(
    limits: dict[str, int],
    model_options: dict[str, int],
    expected_reason: str,
) -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(**limits))
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,), **model_options)
    router.begin("bounded-success")

    result = router.execute("bounded-success", model)

    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert result.decision.requires_human is True
    assert expected_reason in str(result.state.required_action)
    store.close()


def test_model_executor_exceptions_become_bounded_failures() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())
    model = FailingRoutingModel()
    router.begin("provider-failure")

    result = router.execute("provider-failure", model)

    assert model.calls == 1
    assert result.state.current_tier is ModelTier.TERRA
    assert result.state.last_failure_context == (
        "The provider rejected the model call."
    )
    assert result.attempt is not None
    assert result.attempt.outcome is AttemptOutcome.FAILURE
    assert result.attempt.total_tokens == 5
    store.close()


def test_model_execution_skips_provider_when_budget_is_already_exhausted() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_tokens=5))
    model = OverBudgetRoutingModel()
    router.begin("preflight-budget")
    store._connection.execute(
        "UPDATE routing_problems SET total_tokens = ? WHERE problem_id = ?",
        (5, "preflight-budget"),
    )
    before_record_calls: list[str] = []

    result = router.execute(
        "preflight-budget",
        model,
        before_record=lambda: before_record_calls.append("called"),
    )

    assert model.calls == 0
    assert before_record_calls == ["called"]
    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert result.attempt is not None
    assert result.attempt.total_tokens == 0
    assert "Token ceiling reached" in str(result.state.required_action)
    store.close()


def test_routing_configuration_rejects_invalid_limits_and_tiers() -> None:
    with pytest.raises(ValueError, match="token usage"):
        ModelExecution(AttemptOutcome.SUCCESS, input_tokens=-1)
    with pytest.raises(ValueError, match="recursive spawn"):
        ModelExecution(AttemptOutcome.SUCCESS, recursive_spawn_depth=-1)
    with pytest.raises(ValueError, match="model must not be empty"):
        ModelSpec(ModelTier.LUNA, " ")
    with pytest.raises(ValueError, match="model costs"):
        ModelSpec(ModelTier.LUNA, "luna", -1.0)

    for field_name in (
        "max_rounds",
        "max_tokens",
        "max_bounces",
    ):
        with pytest.raises(ValueError):
            RoutingLimits(**{field_name: 0})
    with pytest.raises(ValueError, match="recursive"):
        RoutingLimits(max_recursive_spawn_depth=-1)

    with pytest.raises(ValueError, match="writer"):
        RoutingConfig(writer=ModelSpec(ModelTier.TERRA, "wrong"))
    with pytest.raises(ValueError, match="at least one"):
        RoutingConfig(triage=())
    with pytest.raises(ValueError, match="must not include"):
        RoutingConfig(triage=(ModelSpec(ModelTier.LUNA, "wrong"),))
    with pytest.raises(ValueError, match="unique"):
        RoutingConfig(
            triage=(
                ModelSpec(ModelTier.TERRA, "first"),
                ModelSpec(ModelTier.TERRA, "duplicate"),
            )
        )

    config = build_routing_config()
    with pytest.raises(RoutingError, match="No model"):
        config.spec_for(cast(ModelTier, SimpleNamespace(value="unknown")))
