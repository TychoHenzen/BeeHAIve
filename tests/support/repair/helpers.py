from __future__ import annotations

from pathlib import Path

from beehaiive.review import (
    FindingPublicationState,
    ReaderStatus,
    ReviewConcern,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.workflow import (
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from tests.support.repair.commit_agent import CommitAgent as CommitAgent
from tests.support.repair.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.repair.fixture_review_provider import (
    FixtureReviewProvider as FixtureReviewProvider,
)
from tests.support.repair.passing_check import PassingCheck as PassingCheck
from tests.support.repair.repair_harness import RepairHarness as RepairHarness
from tests.support.repair.repository import (
    git_repository,
    make_repair_repository,
)

CONSTITUTION_PATH = Path(__file__).resolve().parents[3] / "constitution.json"


def repair_harness(
    tmp_path: Path,
    agent: CommitAgent,
    *,
    change_on_call: int | None = None,
) -> RepairHarness:
    repository, remote, base_head, source_head = make_repair_repository(tmp_path)
    provider = FixtureProvider(
        remote,
        repository,
        base_head,
        source_head,
        change_on_call=change_on_call,
    )
    review_provider = FixtureReviewProvider(provider)
    review_store = ReviewStore(tmp_path / "reviews.db")
    reviews = ReviewService(review_store, provider=review_provider)
    cycle = reviews.start_cycle("owner/repo#1", source_head)
    snapshot = reviews.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("selected issue", "unselected issue"),
    )
    selected, unselected = snapshot.findings[-2:]
    with review_store.transaction() as connection:
        connection.executemany(
            "UPDATE review_findings SET publication_state = ? WHERE finding_id = ?",
            (
                (FindingPublicationState.PUBLISHED.value, selected.finding_id),
                (FindingPublicationState.PUBLISHED.value, unselected.finding_id),
            ),
        )
    workflow_store = WorkflowStore(tmp_path / "workflow.db")
    workflow = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        (PassingCheck(),),
    )
    routing_store = RoutingStore(tmp_path / "routing.db")
    service = ReviewRepairService(
        reviews,
        workflow,
        provider,  # type: ignore[arg-type]
        ModelRouter(routing_store),
        agent,  # type: ignore[arg-type]
        tmp_path / "repair-worktrees",
    )
    return RepairHarness(
        repository,
        remote,
        source_head,
        review_store,
        workflow_store,
        routing_store,
        reviews,
        workflow,
        provider,
        agent,
        service,
        cycle.cycle.cycle_id,
        selected.finding_id,
        unselected.finding_id,
    )


def record_pushed_repair(
    harness: RepairHarness, *, push: bool = True
) -> tuple[str, str]:
    attempt, created = harness.reviews.create_repair_attempt(
        harness.cycle_id, (harness.selected_id,), "operator"
    )
    assert created is True
    repaired_file = harness.repository / "manual-repair.txt"
    repaired_file.write_text("fixed\n", encoding="utf-8")
    git_repository(harness.repository, "add", "manual-repair.txt")
    git_repository(harness.repository, "commit", "-m", "manual repair")
    commit_sha = git_repository(harness.repository, "rev-parse", "HEAD")
    if push:
        git_repository(
            harness.repository,
            "push",
            "origin",
            f"{commit_sha}:refs/heads/feature",
        )
    evidence = {
        "repository": "owner/repo",
        "pull_request_number": 1,
        "source_branch": "feature",
        "expected_head": harness.source_head,
        "pushed_head": commit_sha,
    }
    completed = harness.review_store.finish_repair_attempt(
        attempt.attempt_id,
        ReviewRepairStatus.SUCCEEDED,
        commit_sha=commit_sha,
        push_evidence=evidence,
        result="repair committed and pushed",
    )
    assert completed.review_transition_status is ReviewRepairTransitionStatus.PENDING
    return attempt.attempt_id, commit_sha
