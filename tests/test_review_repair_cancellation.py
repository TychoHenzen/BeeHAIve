from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.review import (
    ReviewError,
    ReviewRepairStatus,
)
from tests.support.repair.blocking_agent import BlockingAgent as BlockingAgent
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.helpers import git_repository, repair_harness

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_selected_repair_cancellation_stops_before_push(tmp_path: Path) -> None:
    agent = BlockingAgent()
    harness = repair_harness(tmp_path, agent)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert agent.started.wait(timeout=5)
        harness.service.cancel(attempt.attempt_id, "operator")
        completed = harness.service.wait(attempt.attempt_id, timeout=5)

        assert completed.status is ReviewRepairStatus.CANCELLED
        assert completed.cancellation_requested is True
        assert harness.reviews.snapshot("owner/repo#1").cycle.cycle_id == (
            harness.cycle_id
        )
        assert harness.provider.update_calls == 0
        assert git_repository(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
        assert (
            git_repository(harness.repository, "branch", "--show-current") == "feature"
        )
    finally:
        harness.close()


def test_repair_cancellation_winning_push_claim_prevents_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def cancel_before_claim(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        harness.service.cancel(attempt_id, "operator")
        return original_begin(attempt_id, lease_id, commit_sha, push_evidence)

    monkeypatch.setattr(harness.review_store, "begin_repair_push", cancel_before_claim)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.CANCELLED
        assert completed.cancellation_requested is True
        assert harness.provider.update_calls == 0
        assert git_repository(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_lease_loss_after_push_claim_prevents_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def lose_lease_after_claim(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        started = original_begin(attempt_id, lease_id, commit_sha, push_evidence)
        assert started
        harness.workflow.store.stop_lease(lease_id, "simulated lease loss")
        return True

    monkeypatch.setattr(
        harness.review_store, "begin_repair_push", lose_lease_after_claim
    )
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "lease was lost" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert git_repository(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_repair_cannot_be_cancelled_after_push_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def reject_late_cancel(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        started = original_begin(attempt_id, lease_id, commit_sha, push_evidence)
        with pytest.raises(ReviewError, match="after push starts"):
            harness.service.cancel(attempt_id, "operator")
        return started

    monkeypatch.setattr(harness.review_store, "begin_repair_push", reject_late_cancel)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert completed.cancellation_requested is False
        assert harness.provider.update_calls == 1
    finally:
        harness.close()
