from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.review import (
    ReviewRepairStatus,
)
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.helpers import git_repository, repair_harness

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


@pytest.mark.parametrize("push_completed", [True, False])
def test_review_repair_recovers_an_interrupted_push(
    tmp_path: Path, push_completed: bool
) -> None:
    harness = repair_harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert created is True
        if push_completed:
            repair_file = harness.repository / "recovered.txt"
            repair_file.write_text("recovered\n", encoding="utf-8")
            git_repository(harness.repository, "add", "recovered.txt")
            git_repository(harness.repository, "commit", "-m", "recovered repair")
            commit_sha = git_repository(harness.repository, "rev-parse", "HEAD")
            git_repository(
                harness.repository,
                "push",
                "origin",
                f"{commit_sha}:refs/heads/feature",
            )
        else:
            commit_sha = "f" * 40
        assert harness.review_store.claim_repair_attempt(attempt.attempt_id)
        assert harness.review_store.attach_repair_lease(
            attempt.attempt_id, "missing-lease"
        )
        evidence = {
            "repository": "owner/repo",
            "pull_request_number": 1,
            "source_branch": "feature",
            "expected_head": harness.source_head,
            "pushed_head": commit_sha,
        }
        assert harness.review_store.begin_repair_push(
            attempt.attempt_id, "missing-lease", commit_sha, evidence
        )

        recovered = harness.service.get(attempt.attempt_id)

        assert recovered.status is (
            ReviewRepairStatus.SUCCEEDED
            if push_completed
            else ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        )
        assert recovered.commit_sha == commit_sha
        assert recovered.push_evidence == evidence
        assert harness.provider.update_calls == 0
    finally:
        harness.close()
