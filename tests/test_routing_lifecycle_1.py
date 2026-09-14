from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelSpec,
    ModelTier,
    RoutingDecision,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.routing.blocking_routing_model import (
    BlockingRoutingModel as BlockingRoutingModel,
)
from tests.support.routing.fake_routing_model import (
    FakeRoutingModel as FakeRoutingModel,
)
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_routing_uses_writer_then_escalates_and_returns_concise_context() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config())

    started = router.begin("problem-1")
    assert started.decision.tier is ModelTier.LUNA
    assert started.decision.model == "cheap-writer"
    assert started.decision.reason == "routine"

    first_failure = router.record(
        "problem-1",
        AttemptOutcome.FAILURE,
        input_tokens=100,
        output_tokens=20,
        failure_context="  compiler   failed on the import cycle\nwith details  ",
    )
    assert first_failure.decision.tier is ModelTier.TERRA
    assert first_failure.decision.failure_context == (
        "compiler failed on the import cycle with details"
    )
    assert first_failure.state.consecutive_failures == 1

    triage_retry = router.record(
        "problem-1",
        AttemptOutcome.RETRY,
        input_tokens=40,
        output_tokens=10,
        failure_context="Remove the cycle, then rerun the focused test.",
    )
    assert triage_retry.decision.tier is ModelTier.LUNA
    assert triage_retry.decision.failure_context == (
        "Remove the cycle, then rerun the focused test."
    )

    second_failure = router.record(
        "problem-1",
        AttemptOutcome.FAILURE,
        input_tokens=80,
        output_tokens=20,
        failure_context="The focused test still fails.",
    )
    assert second_failure.decision.tier is ModelTier.SOL
    assert second_failure.state.consecutive_failures == 2
    assert [attempt.tier for attempt in second_failure.attempts] == [
        ModelTier.LUNA,
        ModelTier.TERRA,
        ModelTier.LUNA,
    ]
    assert all(attempt.reason for attempt in second_failure.attempts)
    assert all(attempt.estimated_cost > 0 for attempt in second_failure.attempts)
    assert [attempt.bounce_count for attempt in second_failure.attempts] == [1, 2, 3]

    store.close()


@pytest.mark.parametrize(
    ("outcomes", "expected_models", "expected_status"),
    (
        (
            (AttemptOutcome.FAILURE, AttemptOutcome.SUCCESS),
            ("cheap-writer", "first-triage"),
            RoutingStatus.RESOLVED,
        ),
        (
            (
                AttemptOutcome.FAILURE,
                AttemptOutcome.FAILURE,
                AttemptOutcome.FAILURE,
                AttemptOutcome.FAILURE,
            ),
            ("cheap-writer", "first-triage", "second-triage", "final-triage"),
            RoutingStatus.HUMAN_HANDOFF,
        ),
    ),
)
def test_model_execution_is_table_driven_and_uses_selected_tiers(
    outcomes: tuple[AttemptOutcome, ...],
    expected_models: tuple[str, ...],
    expected_status: RoutingStatus,
) -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_rounds=10, max_bounces=10))
    model = FakeRoutingModel(outcomes, input_tokens=3, output_tokens=2)
    router.begin("model-execution")

    result = router.snapshot("model-execution")
    while result.state.status is RoutingStatus.ACTIVE:
        result = router.execute("model-execution", model)

    assert result.state.status is expected_status
    assert tuple(model.models) == expected_models
    assert tuple(attempt.model for attempt in result.attempts) == expected_models
    assert all(attempt.total_tokens == 5 for attempt in result.attempts)
    store.close()


def test_concurrent_model_execution_serializes_tier_selection() -> None:
    store = RoutingStore()
    router = ModelRouter(store, build_routing_config(max_rounds=10, max_bounces=10))
    model = BlockingRoutingModel()
    router.begin("concurrent-model")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(router.execute, "concurrent-model", model)
        assert model.started.wait(timeout=2)
        second = executor.submit(router.execute, "concurrent-model", model)
        model.release.set()
        first.result()
        second.result()

    snapshot = router.snapshot("concurrent-model")
    assert model.models == ["cheap-writer", "first-triage"]
    assert [attempt.model for attempt in snapshot.attempts] == model.models
    store.close()


def test_successful_model_call_without_task_result_cannot_complete() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    store = OrchestratorStore()
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    service = Orchestrator(store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease)

    routed = service.run_implementation_attempt(run.run_id, lease)

    assert routed.task_result is not None
    assert routed.task_result.outcome is TaskOutcome.FAIL
    assert routed.attempt is not None
    assert routed.attempt.outcome is AttemptOutcome.SUCCESS
    assert routed.state.status is not RoutingStatus.RESOLVED
    with pytest.raises(StoreError, match="Only a passing"):
        store.complete_agent_run(run.run_id, routed.execution_result or "done", lease)
    routing_store.close()
    store.close()


def test_routing_translates_structured_task_outcomes() -> None:
    cases = (
        (TaskResult(TaskOutcome.PASS, {}), RoutingStatus.RESOLVED),
        (
            TaskResult(TaskOutcome.FAIL, {}, validation_reason="checks failed"),
            RoutingStatus.ACTIVE,
        ),
        (
            TaskResult(TaskOutcome.BLOCKED, {}, required_action="Grant access"),
            RoutingStatus.HUMAN_HANDOFF,
        ),
        (
            TaskResult(TaskOutcome.QUESTION, {}, question="Which branch?"),
            RoutingStatus.HUMAN_HANDOFF,
        ),
    )
    store = RoutingStore()
    router = ModelRouter(store)

    class StructuredModel:
        def __init__(self, result: TaskResult) -> None:
            self.result = result

        def execute(
            self, _spec: ModelSpec, _decision: RoutingDecision
        ) -> ModelExecution:
            return ModelExecution(AttemptOutcome.SUCCESS, task_result=self.result)

    try:
        for index, (task_result, status) in enumerate(cases):
            problem_id = f"task-outcome-{index}"
            router.begin(problem_id)
            routed = router.execute(problem_id, StructuredModel(task_result))
            assert routed.state.status is status
            assert routed.task_result == task_result
            assert routed.attempt is not None
            assert routed.attempt.outcome is AttemptOutcome.SUCCESS
    finally:
        store.close()


def test_refine_failure_does_not_start_model_routing_before_implementation() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.fail(run.run_id, "refinement failed", token)
    assert router.store.get_problem(run.run_id) is None

    retried = service.claim("owner:7", "owner/api", "worker-1")
    assert retried is not None
    service.advance(retried.run_id, Stage.IMPLEMENT, retried.lease_token or "")
    assert router.snapshot(run.run_id).decision.tier is ModelTier.LUNA

    routing_store.close()
    orchestrator_store.close()
