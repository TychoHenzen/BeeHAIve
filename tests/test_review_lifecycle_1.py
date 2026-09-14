from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from beehaiive.review import (
    REQUIRED_CONCERNS,
    AllowListReviewAuthorizer,
    PullRequestTarget,
    ReaderStatus,
    ReviewConcern,
    ReviewCycleStatus,
    ReviewError,
    ReviewService,
    ReviewStore,
)
from tests.support.review.blocking_reader import BlockingReader as BlockingReader
from tests.support.review.failing_reader import FailingReader as FailingReader
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import pass_all, reader_doubles


def test_cycle_starts_with_four_pending_readers_and_is_idempotent(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)

    started = service.start_cycle("PR-1", "abc123")
    repeated = service.start_cycle("PR-1", "abc123")

    assert started.cycle.cycle_number == 1
    assert started.cycle.status is ReviewCycleStatus.ACTIVE
    assert [reader.concern for reader in started.readers] == list(REQUIRED_CONCERNS)
    assert all(reader.status is ReaderStatus.PENDING for reader in started.readers)
    assert repeated.cycle.cycle_id == started.cycle.cycle_id
    assert repeated.merge_allowed is False


def test_review_authorization_uses_a_closed_action_policy(
    review_store: ReviewStore,
) -> None:
    authorizer = AllowListReviewAuthorizer()
    assert authorizer.authorize("PR", "reader", "unknown") is False

    store = review_store
    service = ReviewService(store)
    with pytest.raises(ReviewError, match="Unknown review action"):
        service.authorize("PR", "reader", "unknown")


def test_reader_claims_are_durable_and_expire(review_store: ReviewStore) -> None:
    store = review_store
    service = ReviewService(store)
    cycle = service.start_cycle("PR-CLAIM", "head-1")
    claim = store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY)
    assert claim is not None
    assert store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY) is None
    with pytest.raises(ReviewError, match="no longer valid"):
        service.record_reader(
            cycle.cycle.cycle_id,
            ReviewConcern.SECURITY,
            ReaderStatus.PASS,
            claim_token="wrong",
        )

    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE review_readers
            SET claim_expires_at = ?
            WHERE cycle_id = ? AND concern = ?
            """,
            ("not-a-timestamp", cycle.cycle.cycle_id, ReviewConcern.SECURITY.value),
        )
    renewed_claim = store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY)
    assert renewed_claim is not None
    service.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.PASS,
        claim_token=renewed_claim,
    )
    assert store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.SECURITY) is None
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        store.claim_reader("missing", ReviewConcern.SECURITY)

    newer_cycle = service.start_cycle("PR-CLAIM", "head-2")
    with pytest.raises(ReviewError, match="stale review cycle"):
        store.claim_reader(cycle.cycle.cycle_id, ReviewConcern.TEST_COVERAGE)
    service.approve_for_merge(newer_cycle.cycle.cycle_id, "Operator approved")
    with pytest.raises(ReviewError, match="no longer accepting"):
        store.claim_reader(newer_cycle.cycle.cycle_id, ReviewConcern.SECURITY)


def test_concurrent_ready_reviews_claim_each_reader_once(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-CONCURRENT": PullRequestTarget("PR-CONCURRENT", "h1")}
    )
    started = Event()
    release = Event()
    readers = reader_doubles()
    readers[ReviewConcern.SECURITY] = BlockingReader(started, release)
    first = ReviewService(store, provider=provider, readers=readers)
    second = ReviewService(store, provider=provider, readers=readers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.run_ready_review, "PR-CONCURRENT")
        assert started.wait(timeout=2)
        second_future = executor.submit(second.run_ready_review, "PR-CONCURRENT")
        second_result = second_future.result(timeout=2)
        release.set()
        first_result = first_future.result(timeout=2)

    assert first_result.cycle.status is ReviewCycleStatus.PASSED
    assert second_result.cycle.status is ReviewCycleStatus.ACTIVE
    security = next(
        reader
        for reader in second_result.readers
        if reader.concern is ReviewConcern.SECURITY
    )
    assert security.status is ReaderStatus.PENDING
    assert all(reader.calls == 1 for reader in readers.values())


def test_reader_failures_are_persisted_as_failed_results(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-READER-FAIL": PullRequestTarget("PR-READER-FAIL", "h1")}
    )
    readers = reader_doubles()
    readers[ReviewConcern.SECURITY] = FailingReader()
    service = ReviewService(store, provider=provider, readers=readers)

    result = service.run_ready_review("PR-READER-FAIL")

    security = next(
        reader for reader in result.readers if reader.concern is ReviewConcern.SECURITY
    )
    assert result.cycle.status is ReviewCycleStatus.FAILED
    assert security.status is ReaderStatus.FAIL
    assert "reader failed" in result.findings[0].summary


def test_ready_review_rejects_malformed_provider_head_before_persistence(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-BAD-HEAD": PullRequestTarget("PR-BAD-HEAD", " ")})
    service = ReviewService(store, provider=provider, readers=reader_doubles())

    with pytest.raises(ReviewError, match="Provider head SHA"):
        service.run_ready_review("PR-BAD-HEAD")
    with pytest.raises(ReviewError, match="No review cycle"):
        service.snapshot("PR-BAD-HEAD")


def test_stale_review_start_cannot_replace_a_newer_cycle(
    review_store: ReviewStore,
) -> None:
    store = review_store
    service = ReviewService(store)
    initial = service.start_cycle("PR-CAS", "head-0")
    newer = service._start_cycle(
        "PR-CAS", "head-2", expected_cycle_id=initial.cycle.cycle_id
    )

    with pytest.raises(ReviewError, match="changed while starting"):
        service._start_cycle(
            "PR-CAS", "head-1", expected_cycle_id=initial.cycle.cycle_id
        )
    assert service.snapshot("PR-CAS").cycle.cycle_id == newer.cycle.cycle_id


def test_stale_same_head_start_returns_the_current_cycle(
    review_store: ReviewStore,
) -> None:
    service = ReviewService(review_store)
    initial = service._start_cycle("PR-SAME", "head-1", expected_cycle_id=None)

    repeated = service._start_cycle("PR-SAME", "head-1", expected_cycle_id=None)

    assert repeated.cycle.cycle_id == initial.cycle.cycle_id


def test_all_specialized_readers_pass_and_persist_for_merge(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    provider = FixtureProvider({"PR-2": PullRequestTarget("PR-2", "head-1")})
    first_store = ReviewStore(database)
    try:
        first_service = ReviewService(first_store, provider=provider)
        cycle = first_service.start_cycle("PR-2", "head-1")
        pass_all(first_service, cycle.cycle.cycle_id)

        passed = first_service.snapshot("PR-2")
        provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-2")
        with pytest.raises(ReviewError, match="no current review authorization"):
            first_service.merge_handoff("PR-2", "head-1")
        provider.targets["PR-2"] = PullRequestTarget("PR-2", "head-1")
        handoff = first_service.merge_handoff("PR-2", "head-1")
    finally:
        first_store.close()

    second_store = ReviewStore(database)
    try:
        resumed = ReviewService(second_store).snapshot("PR-2")
        assert passed.cycle.status is ReviewCycleStatus.PASSED
        assert passed.merge_allowed is True
        assert handoff.approved_by_human is False
        assert resumed.cycle.cycle_id == cycle.cycle.cycle_id
        assert all(reader.status is ReaderStatus.PASS for reader in resumed.readers)
    finally:
        second_store.close()


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
