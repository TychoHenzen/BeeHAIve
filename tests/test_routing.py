import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelSpec,
    ModelTier,
    RoutingAttempt,
    RoutingConfig,
    RoutingDecision,
    RoutingError,
    RoutingLimits,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app


class RoutingProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id=project_id,
            name="Planning",
            repositories=(
                RepositorySnapshot(
                    "owner/api", (PbiSnapshot("owner/api", 1, "API one"),)
                ),
            ),
        )

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)


class FakeRoutingModel:
    def __init__(
        self,
        outcomes: tuple[AttemptOutcome, ...] = (AttemptOutcome.FAILURE,),
        *,
        input_tokens: int = 1,
        output_tokens: int = 1,
        recursive_spawn_depth: int = 0,
    ) -> None:
        self.calls = 0
        self.outcomes = outcomes
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.recursive_spawn_depth = recursive_spawn_depth
        self.models: list[str] = []

    def run(self) -> AttemptOutcome:
        self.calls += 1
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]

    def execute(self, spec: ModelSpec, _decision: object) -> ModelExecution:
        self.models.append(spec.model)
        outcome = self.run()
        return ModelExecution(
            outcome,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            failure_context="The fake model did not resolve the problem.",
            recursive_spawn_depth=self.recursive_spawn_depth,
        )


class BlockingRoutingModel:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.models: list[str] = []

    def execute(self, spec: ModelSpec, _decision: object) -> ModelExecution:
        self.models.append(spec.model)
        if len(self.models) == 1:
            self.started.set()
            self.release.wait(timeout=2)
        return ModelExecution(
            AttemptOutcome.FAILURE,
            input_tokens=1,
            output_tokens=1,
            failure_context="The concurrent model attempt failed.",
        )


class FailingRoutingModel:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _spec: ModelSpec, _decision: object) -> ModelExecution:
        self.calls += 1
        error = RuntimeError("provider unavailable")
        error.input_tokens = 3  # type: ignore[attr-defined]
        error.output_tokens = 2  # type: ignore[attr-defined]
        error.failure_context = "The provider rejected the model call."  # type: ignore[attr-defined]
        raise error


class UnannotatedFailingRoutingModel:
    def execute(self, _spec: ModelSpec, _decision: object) -> ModelExecution:
        raise RuntimeError("provider unavailable")


class OverBudgetRoutingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.budgets: list[tuple[int, int, int]] = []

    def execute(self, _spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        self.calls += 1
        self.budgets.append(
            (
                decision.remaining_tokens,
                decision.remaining_rounds,
                decision.remaining_recursive_spawn_depth,
            )
        )
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            input_tokens=self.budgets[-1][0] + 1,
        )


def _config(**limits: int) -> RoutingConfig:
    return RoutingConfig(
        writer=ModelSpec(ModelTier.LUNA, "cheap-writer", 1.0, 2.0),
        triage=(
            ModelSpec(ModelTier.TERRA, "first-triage", 2.0, 4.0),
            ModelSpec(ModelTier.SOL, "second-triage", 3.0, 6.0),
            ModelSpec(ModelTier.ASTRA, "final-triage", 4.0, 8.0),
        ),
        limits=RoutingLimits(**limits),
    )


def test_routing_uses_writer_then_escalates_and_returns_concise_context() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config())

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
    router = ModelRouter(store, _config(max_rounds=10, max_bounces=10))
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
    router = ModelRouter(store, _config(max_rounds=10, max_bounces=10))
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


def test_success_resets_state_and_a_new_problem_starts_at_first_triage() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config())

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


def test_triage_exhaustion_requires_human_action() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config(max_rounds=10, max_bounces=10))
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
        router = ModelRouter(store, _config(**limits))
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
    router = ModelRouter(store, _config(**limits))
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
    router = ModelRouter(store, _config(**limits))
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,), **model_options)
    router.begin("bounded-success")

    result = router.execute("bounded-success", model)

    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    assert result.decision.requires_human is True
    assert expected_reason in str(result.state.required_action)
    store.close()


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
    router = ModelRouter(routing_store, _config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    router.begin("shared-problem")
    assert router.snapshot("shared-problem").state.problem_id == "shared-problem"

    conflicting_router_store = RoutingStore()
    with pytest.raises(ValueError, match="share one model router"):
        create_app(
            orchestrator=service,
            model_router=ModelRouter(conflicting_router_store, _config()),
        )
    with pytest.raises(ValueError, match="share one routing store"):
        create_app(orchestrator=service, routing_store=conflicting_router_store)

    standalone_store = RoutingStore()
    standalone_router_store = RoutingStore()
    with pytest.raises(ValueError, match="share one routing store"):
        create_app(
            routing_store=standalone_store,
            model_router=ModelRouter(standalone_router_store, _config()),
        )

    conflicting_router_store.close()
    standalone_store.close()
    standalone_router_store.close()
    routing_store.close()
    orchestrator_store.close()


def test_routing_snapshot_errors_are_returned_by_run_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_run_attempt_api_executes_the_selected_model() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,), input_tokens=7, output_tokens=3)
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
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
    client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )

    attempted = client.post(f"/runs/{run_id}/attempt", headers=lease_headers)

    assert attempted.status_code == 200
    assert attempted.json()["routing"]["attempt"]["model"] == "cheap-writer"
    assert attempted.json()["routing"]["attempt"]["total_tokens"] == 10
    assert attempted.json()["routing"]["state"]["status"] == "resolved"
    assert model.models == ["cheap-writer"]

    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_requires_executor_and_implementation_stage() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, RoutingProvider())
    with pytest.raises(StoreError, match="router and executor"):
        service.run_implementation_attempt("missing", "missing")
    store.close()

    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    with pytest.raises(StoreError, match="implementation run"):
        service.run_implementation_attempt(run.run_id, run.lease_token or "")

    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_surfaces_router_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    model = FakeRoutingModel((AttemptOutcome.SUCCESS,))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router, model)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    def reject_execution(*args: object, **kwargs: object) -> object:
        raise RoutingError("routing execution rejected")

    monkeypatch.setattr(router, "execute", reject_execution)
    with pytest.raises(StoreError, match="routing execution rejected"):
        service.run_implementation_attempt(run.run_id, token)

    routing_store.close()
    orchestrator_store.close()


def test_run_attempt_rejects_a_lost_execution_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_complete_routing_problem_rejects_a_non_resolved_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)
    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)

    def return_active(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(state=SimpleNamespace(status=RoutingStatus.ACTIVE))

    monkeypatch.setattr(router, "record", return_active)
    with pytest.raises(StoreError, match="requires human action"):
        service._complete_routing_problem(run.run_id)

    routing_store.close()
    orchestrator_store.close()


def test_routing_persists_attempts_across_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "routing.sqlite3"
    first_store = RoutingStore(database)
    first_router = ModelRouter(first_store, _config())
    first_router.begin("persisted-problem")
    first_router.record(
        "persisted-problem",
        AttemptOutcome.FAILURE,
        input_tokens=12,
        output_tokens=8,
        failure_context="The persisted attempt failed.",
        recursive_spawn_depth=1,
    )
    first_store.close()

    second_store = RoutingStore(database)
    resumed = ModelRouter(second_store, _config()).snapshot("persisted-problem")

    assert resumed.state.current_tier is ModelTier.TERRA
    assert resumed.state.consecutive_failures == 1
    assert resumed.state.total_tokens == 20
    assert len(resumed.attempts) == 1
    attempt = resumed.attempts[0]
    assert attempt.model == "cheap-writer"
    assert attempt.tier is ModelTier.LUNA
    assert attempt.reason == "routine"
    assert attempt.input_tokens == 12
    assert attempt.output_tokens == 8
    assert attempt.total_tokens == 20
    assert attempt.estimated_cost > 0
    assert attempt.bounce_count == 1
    assert attempt.recursive_spawn_depth == 1
    second_store.close()


def test_routing_store_migrates_and_replays_transition_ids(tmp_path: Path) -> None:
    database = tmp_path / "legacy-routing.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE routing_problems(
            problem_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            current_tier TEXT NOT NULL, triage_index INTEGER NOT NULL,
            consecutive_failures INTEGER NOT NULL, bounce_count INTEGER NOT NULL,
            round INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
            total_cost REAL NOT NULL, recursive_spawn_depth INTEGER NOT NULL,
            last_failure_context TEXT, required_action TEXT,
            next_reason TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE routing_attempts(
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id TEXT NOT NULL, round INTEGER NOT NULL,
            model TEXT NOT NULL, tier TEXT NOT NULL, reason TEXT NOT NULL,
            outcome TEXT NOT NULL, input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
            estimated_cost REAL NOT NULL, bounce_count INTEGER NOT NULL,
            recursive_spawn_depth INTEGER NOT NULL, failure_context TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (problem_id) REFERENCES routing_problems(problem_id)
        );
        """
    )
    connection.close()

    store = RoutingStore(database)
    columns = {
        str(row["name"])
        for row in store._connection.execute("PRAGMA table_info(routing_attempts)")
    }
    assert "transition_id" in columns

    router = ModelRouter(store, _config())
    started = router.begin("transition-problem")
    recorded = router.record(
        "transition-problem", AttemptOutcome.SUCCESS, transition_id="transition-1"
    )
    assert recorded.attempt is not None
    replayed = store.save_transition(
        "transition-problem",
        started.state.round,
        recorded.state,
        recorded.attempt,
        "transition-1",
    )
    assert replayed == (recorded.state, recorded.attempt)
    with pytest.raises(RoutingError, match="belongs to another problem"):
        store.save_transition(
            "other-problem",
            started.state.round,
            recorded.state,
            recorded.attempt,
            "transition-1",
        )

    store._connection.execute("PRAGMA foreign_keys = OFF")
    store._connection.execute(
        "DELETE FROM routing_problems WHERE problem_id = ?", ("transition-problem",)
    )
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        store.save_transition(
            "transition-problem",
            started.state.round,
            recorded.state,
            recorded.attempt,
            "transition-1",
        )
    store.close()


def test_orchestrator_connects_implementation_failures_to_model_router() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_refine_failure_does_not_start_model_routing_before_implementation() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_successful_handoff_resets_the_connected_routing_problem() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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
    router = ModelRouter(routing_store, _config(max_rounds=1, max_bounces=10))
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
    service.fail(run.run_id, "implementation failed", token)
    handoff = router.snapshot(run.run_id)
    assert handoff.state.status is RoutingStatus.HUMAN_HANDOFF

    assert service.claim("owner:7", "owner/api", "worker-1") is None
    pbi = orchestrator_store.project_state("owner:7")["repositories"][0]["pbis"][0]
    assert pbi["claimable"] is False
    assert handoff.state.required_action

    routing_store.close()
    orchestrator_store.close()


def test_resolved_routing_problem_can_be_reopened_after_run_persistence_failure() -> (
    None
):
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    resolved = router.begin("recover-me")
    resolved = router.record("recover-me", AttemptOutcome.SUCCESS)

    with pytest.raises(RoutingError, match="required"):
        router.reopen_resolved("recover-me", " ")
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        routing_store.reopen_problem("missing", 0, resolved.state)
    with pytest.raises(RoutingError, match="changed during recovery"):
        routing_store.reopen_problem("recover-me", 99, resolved.state)

    reopened = router.reopen_resolved("recover-me", "result persistence failed")
    assert reopened.state.status is RoutingStatus.ACTIVE
    assert reopened.state.last_failure_context == "result persistence failed"
    assert (
        router.reopen_resolved("recover-me", "ignored").state.status
        is RoutingStatus.ACTIVE
    )
    routing_store.close()


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


def test_routing_snapshot_endpoint_maps_errors_and_rejects_missing_path_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    orchestrator_store = OrchestratorStore()
    router = ModelRouter(routing_store, _config())
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


def test_long_model_execution_renews_shared_run_lease(tmp_path: Path) -> None:
    state_database = tmp_path / "state.sqlite3"
    routing_database = tmp_path / "routing.sqlite3"
    first_store = OrchestratorStore(state_database, lease_seconds=1)
    first_router = ModelRouter(RoutingStore(routing_database), _config())
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
            ModelRouter(RoutingStore(routing_database), _config()),
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


def test_reclaimed_run_rejects_stale_model_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_database = tmp_path / "state.sqlite3"
    routing_database = tmp_path / "routing.sqlite3"
    first_store = OrchestratorStore(state_database, lease_seconds=1)
    first_router = ModelRouter(RoutingStore(routing_database), _config())
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
            ModelRouter(RoutingStore(routing_database), _config()),
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
    router = ModelRouter(routing_store, _config(max_rounds=1, max_bounces=10))
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
    router = ModelRouter(routing_store, _config())
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


def test_failed_run_retries_routing_persistence_after_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
    orchestrator_store = OrchestratorStore()
    service = Orchestrator(orchestrator_store, RoutingProvider(), router)

    service.synchronize("owner:7")
    run = service.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, token)
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
        service.fail(run.run_id, "implementation failed", token)

    retried = service.fail(
        run.run_id,
        "implementation failed again",
        token,
        input_tokens=4,
        output_tokens=2,
    )
    assert retried.status.value == "failed"
    assert calls == 2
    assert len(router.snapshot(run.run_id).attempts) == 1

    routing_store.close()
    orchestrator_store.close()


def test_later_failure_replays_only_its_own_pending_routing_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_failed_run_treats_post_commit_routing_error_as_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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
    router = ModelRouter(routing_store, _config())
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
    router = ModelRouter(routing_store, _config())
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


def test_handoff_surfaces_routing_success_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())

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
    router = ModelRouter(store, _config(max_rounds=3, max_bounces=10))
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


def test_model_executor_exceptions_become_bounded_failures() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config())
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


def test_unannotated_model_exception_gets_a_bounded_failure_context() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config())
    router.begin("unannotated-provider-failure")

    result = router.execute(
        "unannotated-provider-failure", UnannotatedFailingRoutingModel()
    )

    assert (
        result.state.last_failure_context
        == "Model execution failed: provider unavailable"
    )
    store.close()


def test_model_executor_receives_budgets_and_overuse_stops_at_human_handoff() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config(max_tokens=5))
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


def test_model_execution_skips_provider_when_budget_is_already_exhausted() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config(max_tokens=5))
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


def test_orchestrator_surfaces_routing_transition_error() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())
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


def test_orchestrator_surfaces_routing_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, _config())

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

    config = _config()
    with pytest.raises(RoutingError, match="No model"):
        config.spec_for(cast(ModelTier, SimpleNamespace(value="unknown")))


def test_routing_rejects_invalid_transitions_and_persists_transaction_failures() -> (
    None
):
    store = RoutingStore()
    router = ModelRouter(store, _config())
    started = router.begin("problem-1")

    with pytest.raises(RoutingError, match="already exists"):
        router.begin("problem-1")
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        router.snapshot("missing")
    with pytest.raises(RoutingError, match="Unknown routing problem"):
        router.handoff_limit_reason("missing")
    with pytest.raises(RoutingError, match="Unknown attempt outcome"):
        router.record("problem-1", "unknown")
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


def test_bounce_limit_and_long_context_are_recorded() -> None:
    store = RoutingStore()
    router = ModelRouter(store, _config(max_rounds=10, max_bounces=1))
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
