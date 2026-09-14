from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from beehaiive.models import PullRequestSnapshot
from beehaiive.review import (
    ReviewCycleStatus,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import ModelRouter
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.fixture_review_provider import (
    FixtureReviewProvider as FixtureReviewProvider,
)
from tests.support.repair.helpers import record_pushed_repair, repair_harness

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_review_repair_recovers_a_queued_attempt_after_restart(
    tmp_path: Path,
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        restarted = ReviewRepairService(
            harness.reviews,
            harness.workflow,
            harness.provider,  # type: ignore[arg-type]
            ModelRouter(harness.routing_store),
            harness.agent,  # type: ignore[arg-type]
            tmp_path / "repair-worktrees",
        )
        assert created is True
        assert attempt.status is ReviewRepairStatus.QUEUED

        restarted.recover()
        completed = restarted.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert harness.agent.calls == 1
        repeated = restarted.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert repeated.attempt_id == attempt.attempt_id
        assert harness.agent.calls == 1
    finally:
        harness.close()


def test_review_transition_recovers_after_restart_and_concurrent_delivery(
    tmp_path: Path,
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        attempt_id, _ = record_pushed_repair(harness)
        harness.review_store.close()
        reopened_store = ReviewStore(tmp_path / "reviews.db")
        harness.review_store = reopened_store
        harness.reviews = ReviewService(
            reopened_store, provider=FixtureReviewProvider(harness.provider)
        )
        restarted = ReviewRepairService(
            harness.reviews,
            harness.workflow,
            harness.provider,  # type: ignore[arg-type]
            ModelRouter(harness.routing_store),
            harness.agent,  # type: ignore[arg-type]
            tmp_path / "repair-worktrees",
        )
        barrier = Barrier(2)

        def deliver():
            barrier.wait(timeout=5)
            return restarted.get(attempt_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                future.result(timeout=10)
                for future in (executor.submit(deliver), executor.submit(deliver))
            )

        current = harness.reviews.snapshot("owner/repo#1")
        old = reopened_store.snapshot(harness.cycle_id)
        cycles = reopened_store._connection.execute(
            "SELECT COUNT(*) AS total FROM review_cycles WHERE pull_request_id = ?",
            ("owner/repo#1",),
        ).fetchone()
        assert all(
            result.review_transition_status is ReviewRepairTransitionStatus.COMPLETED
            for result in results
        )
        assert {result.review_transition_cycle_id for result in results} == {
            current.cycle.cycle_id
        }
        assert current.cycle.cycle_number == 2
        assert current.cycle.status is ReviewCycleStatus.ACTIVE
        assert old.cycle.status is ReviewCycleStatus.SUPERSEDED
        assert cycles is not None and int(cycles["total"]) == 2
        restarted.recover()
        assert (
            reopened_store._connection.execute(
                "SELECT COUNT(*) FROM review_cycles WHERE pull_request_id = ?",
                ("owner/repo#1",),
            ).fetchone()[0]
            == 2
        )
    finally:
        harness.close()


@pytest.mark.parametrize(
    "case", ["missing_evidence", "provider_mismatch", "stale_cycle"]
)
def test_review_transition_requires_current_pushed_evidence(
    tmp_path: Path, case: str
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        if case == "missing_evidence":
            attempt, created = harness.reviews.create_repair_attempt(
                harness.cycle_id, (harness.selected_id,), "operator"
            )
            assert created is True
            attempt_id = harness.review_store.finish_repair_attempt(
                attempt.attempt_id, ReviewRepairStatus.SUCCEEDED
            ).attempt_id
            expected_cycles = 1
        else:
            attempt_id, _ = record_pushed_repair(
                harness, push=case != "provider_mismatch"
            )
            expected_cycles = 1
            if case == "stale_cycle":
                harness.reviews._start_cycle(
                    "owner/repo#1",
                    "newer-head",
                    expected_cycle_id=harness.cycle_id,
                )
                expected_cycles = 2

        result = harness.service.get(attempt_id)
        current = harness.reviews.snapshot("owner/repo#1")
        assert result.review_transition_status is (
            ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED
        )
        assert result.review_transition_required_action
        assert len(result.review_transition_required_action) <= 1_000
        assert current.as_dict()["repair"]["review_transition"]["status"] == (
            "human_action_required"
        )
        count = harness.review_store._connection.execute(
            "SELECT COUNT(*) FROM review_cycles WHERE pull_request_id = ?",
            ("owner/repo#1",),
        ).fetchone()[0]
        assert count == expected_cycles
    finally:
        harness.close()


def test_review_transition_retries_provider_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        attempt_id, _ = record_pushed_repair(harness)
        original_get = harness.provider.get_pull_request
        calls = 0

        def fail_once(repository: str, number: int) -> PullRequestSnapshot:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary provider failure")
            return original_get(repository, number)

        monkeypatch.setattr(harness.provider, "get_pull_request", fail_once)
        first = harness.service.get(attempt_id)
        assert first.review_transition_status is (
            ReviewRepairTransitionStatus.RETRY_REQUIRED
        )
        assert len(first.review_transition_required_action or "") <= 1_000
        assert harness.reviews.snapshot("owner/repo#1").cycle.cycle_number == 1

        retried = harness.service.get(attempt_id)

        assert (
            retried.review_transition_status is ReviewRepairTransitionStatus.COMPLETED
        )
        assert retried.review_transition_cycle_id is not None
        assert harness.reviews.snapshot("owner/repo#1").cycle.cycle_number == 2
    finally:
        harness.close()
