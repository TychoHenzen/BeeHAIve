from collections.abc import Iterator

import pytest

from beehaiive.review import (
    FindingPublicationState,
    FindingStatus,
    ReaderStatus,
    ReviewConcern,
    ReviewError,
    ReviewService,
    ReviewStore,
)


def test_review_rejects_invalid_or_stale_transitions(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-ERR", "head-1")

    with pytest.raises(ReviewError, match="required"):
        service.start_cycle(" ", "head-1")
    with pytest.raises(ReviewError, match="at most"):
        service.start_cycle("PR-LONG", "x" * 201)
    with pytest.raises(ReviewError, match="invalid format"):
        service.start_cycle("PR-INVALID", "head*1")
    with pytest.raises(ReviewError, match="Unknown review concern"):
        service.record_reader(cycle.cycle.cycle_id, "unknown", ReaderStatus.PASS)
    with pytest.raises(ReviewError, match="Unknown reader status"):
        service.record_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY, "unknown")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.record_reader("missing", ReviewConcern.SECURITY, ReaderStatus.PASS)
    with pytest.raises(ReviewError, match="failed reader"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.FAIL
        )
    with pytest.raises(ReviewError, match="Only a failed"):
        service.record_reader(
            cycle.cycle.cycle_id,
            ReviewConcern.TEST_COVERAGE,
            ReaderStatus.PASS,
            ("not allowed",),
        )
    service.record_reader(
        cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
    )
    with pytest.raises(ReviewError, match="already recorded"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="Unknown review finding"):
        service.resolve_finding("missing", "none")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.approve_for_merge("missing", "reason")
    with pytest.raises(ReviewError, match="approval reason"):
        service.approve_for_merge(cycle.cycle.cycle_id, " ")
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.add_finding("missing", ReviewConcern.SECURITY, "finding")
    service.approve_for_merge(cycle.cycle.cycle_id, "Operator approval")
    with pytest.raises(ReviewError, match="no longer accepting reader"):
        service.record_reader(
            cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="no longer accepting findings"):
        service.add_finding(
            cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE, "finding"
        )


def test_review_reports_stale_and_missing_reader_records(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    first = service.start_cycle("PR-STALE", "head-1")
    second = service.start_cycle("PR-STALE", "head-2")
    with pytest.raises(ReviewError, match="stale"):
        service.add_finding(first.cycle.cycle_id, ReviewConcern.SECURITY, "old finding")

    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM review_readers WHERE cycle_id = ? AND concern = ?",
            (second.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    with pytest.raises(ReviewError, match="No reader"):
        service.record_reader(
            second.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="No reader"):
        store.claim_reader(second.cycle.cycle_id, ReviewConcern.SECURITY)
    with pytest.raises(ReviewError, match="No reader"):
        service.add_finding(
            second.cycle.cycle_id, ReviewConcern.SECURITY, "missing reader"
        )


def test_review_repair_selection_is_authorized_current_and_idempotent(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    cycle = service.start_cycle("PR-REPAIR", "head-1")
    first = service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("selected issue",),
    ).findings[-1]
    second = service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.PERFORMANCE,
        ReaderStatus.FAIL,
        ("unselected issue",),
    ).findings[-1]
    with review_store.transaction() as connection:
        connection.executemany(
            "UPDATE review_findings SET publication_state = ? WHERE finding_id = ?",
            (
                (FindingPublicationState.PUBLISHED.value, first.finding_id),
                (FindingPublicationState.PUBLISHED.value, second.finding_id),
            ),
        )

    attempt, created = service.create_repair_attempt(
        cycle.cycle.cycle_id, (first.finding_id,), "operator"
    )
    retry, retry_created = service.create_repair_attempt(
        cycle.cycle.cycle_id, (first.finding_id,), "operator"
    )

    assert created is True
    assert retry_created is False
    assert retry.attempt_id == attempt.attempt_id
    assert attempt.finding_ids == (first.finding_id,)
    assert attempt.actor == "operator"
    assert attempt.head_sha == "head-1"
    assert review_store.finding_for_id(second.finding_id).status is FindingStatus.OPEN
    with pytest.raises(ReviewError, match="already claimed"):
        service.create_repair_attempt(
            cycle.cycle.cycle_id, (second.finding_id,), "operator"
        )
    with pytest.raises(ReviewError, match="not authorized"):
        service.create_repair_attempt(
            cycle.cycle.cycle_id, (second.finding_id,), "writer"
        )


def test_review_repair_selection_rejects_duplicates_stale_and_unpublished(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    cycle = service.start_cycle("PR-REPAIR-GUARDS", "head-1")
    finding = service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("selected issue",),
    ).findings[-1]

    with pytest.raises(ReviewError, match="Duplicate finding identifiers"):
        service.create_repair_attempt(
            cycle.cycle.cycle_id,
            (finding.finding_id, finding.finding_id),
            "operator",
        )
    with pytest.raises(ReviewError, match="published"):
        service.create_repair_attempt(
            cycle.cycle.cycle_id, (finding.finding_id,), "operator"
        )

    with review_store.transaction() as connection:
        connection.execute(
            "UPDATE review_findings SET publication_state = ?, stale = 1 "
            "WHERE finding_id = ?",
            (FindingPublicationState.PUBLISHED.value, finding.finding_id),
        )
    with pytest.raises(ReviewError, match="Stale findings"):
        service.create_repair_attempt(
            cycle.cycle.cycle_id, (finding.finding_id,), "operator"
        )


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
