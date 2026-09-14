from collections.abc import Iterator

import pytest

from beehaiive.review import (
    REQUIRED_CONCERNS,
    FindingPublicationState,
    FindingStatus,
    PullRequestTarget,
    ReaderStatus,
    ReviewConcern,
    ReviewCycleStatus,
    ReviewError,
    ReviewService,
    ReviewStore,
)
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import pass_all
from tests.support.review.publishing_fixture_provider import (
    PublishingFixtureProvider as PublishingFixtureProvider,
)


def test_structured_findings_keep_identity_anchor_staleness_and_resolution_evidence(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    first_cycle = service.start_cycle("PR-STRUCTURED", "head-1")
    first_snapshot = service.add_finding(
        first_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    first = first_snapshot.findings[0]
    repeated = service.add_finding(
        first_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    assert len(repeated.findings) == 1
    assert repeated.findings[0].finding_id == first.finding_id

    next_cycle = service.start_cycle("PR-STRUCTURED", "head-2")
    second_snapshot = service.add_finding(
        next_cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Validate the supplied path.",
        file_path="src/app.py",
        start_line=12,
        end_line=14,
    )
    old = next(
        item for item in second_snapshot.findings if item.finding_id == first.finding_id
    )
    second = next(
        item for item in second_snapshot.findings if item.finding_id != first.finding_id
    )
    resolved = service.resolve_finding(first.finding_id, "Addressed", actor="operator")
    dismissed = next(
        item for item in resolved.findings if item.finding_id == first.finding_id
    )

    assert old.stale is True
    assert second.fingerprint == first.fingerprint
    assert second.head_sha == "head-2"
    assert second.first_seen_cycle_id == first_cycle.cycle.cycle_id
    assert second.file_path == "src/app.py"
    assert (second.start_line, second.end_line) == (12, 14)
    assert dismissed.resolution_actor == "operator"
    assert dismissed.resolution_at is not None
    with pytest.raises(ReviewError, match="Resolved findings cannot be published"):
        service.publish_finding(first.finding_id)


def test_finding_publication_retries_safely_and_deduplicates(
    review_store: ReviewStore,
) -> None:
    target = PullRequestTarget("owner/repo#1", "publish-head")
    provider = PublishingFixtureProvider(
        review_store, {target.pull_request_id: target}, fail_once=True
    )
    service = ReviewService(review_store, provider=provider)
    cycle = service.start_cycle(target.pull_request_id, target.head_sha)
    first_snapshot = service.add_finding(
        cycle.cycle.cycle_id, ReviewConcern.SECURITY, "Unsafe URL redirect."
    )
    first = first_snapshot.findings[0]

    retry = service.publish_finding(first.finding_id)
    retried_finding = next(
        item for item in retry.findings if item.finding_id == first.finding_id
    )
    assert retried_finding.publication_state is FindingPublicationState.RETRYABLE
    assert retried_finding.publication_retry_evidence == {"error_type": "RuntimeError"}

    published = service.publish_finding(first.finding_id)
    duplicate = service.add_finding(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        "Same issue reported by another reader.",
        duplicate_target=first.finding_id,
    )
    duplicate_finding = next(
        item for item in duplicate.findings if item.duplicate_target == first.finding_id
    )
    reconciled = service.publish_finding(duplicate_finding.finding_id)
    service.publish_finding(first.finding_id)
    canonical = next(
        item for item in reconciled.findings if item.finding_id == first.finding_id
    )
    duplicate_result = next(
        item
        for item in reconciled.findings
        if item.finding_id == duplicate_finding.finding_id
    )

    assert provider.publish_calls == 3
    assert provider.remote_ids == [None, None, "REMOTE-REVIEW-1"]
    assert provider.in_transaction_during_publish is False
    assert canonical.remote_id == "REMOTE-REVIEW-1"
    assert duplicate_result.publication_state is FindingPublicationState.DUPLICATE
    assert duplicate_result.remote_id == canonical.remote_id
    assert published.merge_allowed is False

    provider.publication_state = FindingPublicationState.REMOTE_MISSING
    missing = service.publish_finding(first.finding_id)
    missing_finding = next(
        item for item in missing.findings if item.finding_id == first.finding_id
    )
    assert provider.publish_calls == 4
    assert provider.remote_ids[-1] == "REMOTE-REVIEW-1"
    assert missing_finding.status is FindingStatus.OPEN
    assert missing_finding.resolution is None
    assert missing_finding.publication_state is FindingPublicationState.REMOTE_MISSING
    assert missing_finding.remote_id == "REMOTE-REVIEW-1"


def test_failed_finding_reaches_writer_and_new_cycle_can_pass(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-3": PullRequestTarget("PR-3", "head-1")})
    service = ReviewService(store, provider=provider)
    first = service.start_cycle("PR-3", "head-1")
    failed = service.record_reader(
        first.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("SQL injection in the query filter",),
        reader="security-reader",
    )

    finding = failed.findings[0]
    assert failed.cycle.status is ReviewCycleStatus.FAILED
    assert failed.readers[0].status is ReaderStatus.FAIL
    assert failed.merge_allowed is False
    assert failed.as_dict()["writer_feedback"] == [finding.as_dict()]

    for concern in REQUIRED_CONCERNS[1:]:
        failed = service.record_reader(first.cycle.cycle_id, concern, ReaderStatus.PASS)
    assert failed.cycle.status is ReviewCycleStatus.FAILED
    assert failed.merge_allowed is False

    resolved = service.resolve_finding(finding.finding_id, "Parameterized the filter")
    assert resolved.findings[0].status is FindingStatus.RESOLVED
    assert resolved.merge_allowed is False

    second = service.start_cycle("PR-3", "head-1")
    assert second.cycle.cycle_number == 2
    assert (
        service.store.snapshot(first.cycle.cycle_id).cycle.status
        is ReviewCycleStatus.SUPERSEDED
    )
    pass_all(service, second.cycle.cycle_id)
    assert service.merge_handoff("PR-3", "head-1").cycle_id == second.cycle.cycle_id


def test_changed_finding_invalidates_a_previous_pass(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider({"PR-5": PullRequestTarget("PR-5", "head-1")})
    service = ReviewService(store, provider=provider)
    cycle = service.start_cycle("PR-5", "head-1")
    pass_all(service, cycle.cycle.cycle_id)

    changed = service.add_finding(
        cycle.cycle.cycle_id,
        ReviewConcern.PERFORMANCE,
        "The query scans the full pull-request history",
    )
    assert changed.cycle.status is ReviewCycleStatus.FAILED
    assert changed.readers[-1].status is ReaderStatus.FAIL
    with pytest.raises(ReviewError, match="Writer must"):
        service.merge_handoff("PR-5", "head-1")

    finding = changed.findings[-1]
    service.resolve_finding(finding.finding_id, "Added an indexed query")
    with pytest.raises(ReviewError, match="Writer must"):
        service.merge_handoff("PR-5", "head-1")
    approved = service.approve_for_merge(
        cycle.cycle.cycle_id, "Accepted the residual risk"
    )
    assert approved.merge_allowed is True


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
