from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from beehaiive.review import (
    ReaderStatus,
    ReviewCycleStatus,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
)
from tests.support.repair.blocking_agent import BlockingAgent as BlockingAgent
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.failing_agent import FailingAgent as FailingAgent
from tests.support.repair.helpers import git_repository, repair_harness
from tests.support.repair.remote_changing_agent import (
    RemoteChangingAgent as RemoteChangingAgent,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_selected_repair_pushes_one_committed_change_without_touching_checkout(
    tmp_path: Path,
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        original_status = git_repository(harness.repository, "status", "--porcelain")
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)
        repeated = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        remote_head = git_repository(harness.remote, "rev-parse", "refs/heads/feature")
        current_review = harness.reviews.snapshot("owner/repo#1")

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert completed.review_transition_status is (
            ReviewRepairTransitionStatus.COMPLETED
        )
        assert completed.review_transition_cycle_id == current_review.cycle.cycle_id
        assert completed.finding_ids == (harness.selected_id,)
        assert completed.head_sha == harness.source_head
        assert completed.actor == "operator"
        assert completed.commit_sha == remote_head
        assert completed.push_evidence is not None
        assert completed.push_evidence["pushed_head"] == remote_head
        assert harness.agent.calls == 1
        assert "selected issue" in harness.agent.prompt
        assert "unselected issue" not in harness.agent.prompt
        assert repeated.attempt_id == completed.attempt_id
        assert repeated.status is ReviewRepairStatus.SUCCEEDED
        assert (
            repeated.review_transition_cycle_id == completed.review_transition_cycle_id
        )
        assert current_review.cycle.cycle_number == 2
        assert current_review.cycle.head_sha == remote_head
        assert current_review.cycle.status is ReviewCycleStatus.ACTIVE
        assert all(
            reader.status is ReaderStatus.PENDING for reader in current_review.readers
        )
        assert harness.review_store.snapshot(harness.cycle_id).cycle.status is (
            ReviewCycleStatus.SUPERSEDED
        )
        assert current_review.merge_allowed is False
        assert current_review.as_dict()["repair"]["review_transition"] == {
            "status": "completed",
            "cycle_id": current_review.cycle.cycle_id,
            "required_action": None,
        }
        assert harness.provider.update_calls == 1
        assert (
            git_repository(harness.repository, "branch", "--show-current") == "feature"
        )
        assert (
            git_repository(harness.repository, "rev-parse", "HEAD")
            == harness.source_head
        )
        assert (
            git_repository(harness.repository, "status", "--porcelain")
            == original_status
        )
        assert (
            harness.reviews.store.finding_for_id(harness.unselected_id).status.value
            == "open"
        )
    finally:
        harness.close()


def test_selected_repair_redacts_secret_finding_context(tmp_path: Path) -> None:
    agent = CommitAgent()
    agent._secret_values = ("repair-secret",)
    harness = repair_harness(tmp_path, agent)
    try:
        with harness.review_store.transaction() as connection:
            connection.execute(
                "UPDATE review_findings SET summary = ? WHERE finding_id = ?",
                ("selected token=repair-secret", harness.selected_id),
            )
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert "repair-secret" not in agent.prompt
        assert "token=[redacted]" in agent.prompt
        assert "unselected issue" not in agent.prompt
    finally:
        harness.close()


def test_concurrent_repair_dispatch_starts_one_worker(tmp_path: Path) -> None:
    agent = BlockingAgent()
    harness = repair_harness(tmp_path, agent)
    barrier = Barrier(2)

    def dispatch():
        barrier.wait(timeout=5)
        return harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = tuple(
                future.result(timeout=5)
                for future in (executor.submit(dispatch), executor.submit(dispatch))
            )
        assert attempts[0].attempt_id == attempts[1].attempt_id
        assert agent.started.wait(timeout=5)
        harness.service.cancel(attempts[0].attempt_id, "operator")
        completed = harness.service.wait(attempts[0].attempt_id, timeout=5)

        assert completed.status is ReviewRepairStatus.CANCELLED
        assert agent.calls == 1
        assert harness.provider.update_calls == 0
    finally:
        harness.close()


def test_selected_repair_refuses_a_changed_source_head(tmp_path: Path) -> None:
    harness = repair_harness(tmp_path, CommitAgent(), change_on_call=4)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "identity changed" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert git_repository(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
        assert (
            git_repository(harness.repository, "branch", "--show-current") == "feature"
        )
        assert (
            git_repository(harness.repository, "rev-parse", "HEAD")
            == harness.source_head
        )
    finally:
        harness.close()


def test_selected_repair_refuses_a_changed_push_remote(tmp_path: Path) -> None:
    harness = repair_harness(tmp_path, RemoteChangingAgent())
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "push remote does not match" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert git_repository(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_failed_repair_does_not_start_a_review_cycle(tmp_path: Path) -> None:
    harness = repair_harness(tmp_path, FailingAgent())
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.FAILED
        assert completed.review_transition_status is (
            ReviewRepairTransitionStatus.NOT_REQUIRED
        )
        assert harness.reviews.snapshot("owner/repo#1").cycle.cycle_id == (
            harness.cycle_id
        )
    finally:
        harness.close()


def test_review_repair_marks_a_stopped_worker_as_actionable(tmp_path: Path) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert created is True
        assert harness.review_store.claim_repair_attempt(attempt.attempt_id)
        assert harness.review_store.attach_repair_lease(
            attempt.attempt_id, "missing-lease"
        )

        recovered = harness.service.get(attempt.attempt_id)

        assert recovered.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "stopped before completion" in (recovered.required_action or "")
        assert harness.agent.calls == 0
    finally:
        harness.close()
