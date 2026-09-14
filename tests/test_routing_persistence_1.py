import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider as RoutingProvider

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_complete_routing_problem_rejects_a_non_resolved_transition(
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
    first_router = ModelRouter(first_store, build_routing_config())
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
    resumed = ModelRouter(second_store, build_routing_config()).snapshot(
        "persisted-problem"
    )

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

    router = ModelRouter(store, build_routing_config())
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


def test_resolved_routing_problem_can_be_reopened_after_run_persistence_failure() -> (
    None
):
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
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


def test_failed_run_retries_routing_persistence_after_transient_error(
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
