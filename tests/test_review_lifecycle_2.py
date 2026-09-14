from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    AllowListReviewAuthorizer,
    PullRequestTarget,
    ReaderStatus,
    ReviewConcern,
    ReviewCycleStatus,
    ReviewError,
    ReviewService,
    ReviewStore,
)
from main import create_app
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import pass_all, reader_doubles
from tests.support.review.lock_inspecting_provider import (
    LockInspectingProvider as LockInspectingProvider,
)


def test_stale_results_cannot_authorize_a_new_head(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider({"PR-4": PullRequestTarget("PR-4", "head-1")})
    service = ReviewService(store, provider=provider)
    first = service.start_cycle("PR-4", "head-1")
    pass_all(service, first.cycle.cycle_id)
    provider.targets["PR-4"] = PullRequestTarget("PR-4", "head-2")
    second = service.start_cycle("PR-4", "head-2")

    with pytest.raises(ReviewError, match="stale"):
        service.record_reader(
            first.cycle.cycle_id, ReviewConcern.SECURITY, ReaderStatus.PASS
        )
    with pytest.raises(ReviewError, match="no current review authorization"):
        service.merge_handoff("PR-4", "head-1")
    with pytest.raises(ReviewError, match="stale"):
        service.approve_for_merge(first.cycle.cycle_id, "Old head approval")
    with pytest.raises(ReviewError, match="Awaiting"):
        service.merge_handoff("PR-4", "head-2")

    approved = service.approve_for_merge(
        second.cycle.cycle_id, "Operator reviewed the risk"
    )
    handoff = service.merge_handoff("PR-4", "head-2")
    assert approved.cycle.status is ReviewCycleStatus.HUMAN_APPROVED
    assert handoff.approved_by_human is True


def test_ready_review_runs_all_injected_readers_and_requires_all_readers(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider(
        {
            "PR-READY": PullRequestTarget("PR-READY", "ready-1"),
            "PR-MISSING": PullRequestTarget("PR-MISSING", "missing-1"),
        }
    )
    readers = reader_doubles()
    service = ReviewService(store, provider=provider, readers=readers)

    completed = service.run_ready_review("PR-READY")

    assert completed.cycle.status is ReviewCycleStatus.PASSED
    assert completed.merge_allowed is True
    assert all(reader.calls == 1 for reader in readers.values())
    assert all(reader.status is ReaderStatus.PASS for reader in completed.readers)

    repeated = service.run_ready_review("PR-READY")
    assert repeated.cycle.cycle_id == completed.cycle.cycle_id
    assert all(reader.calls == 1 for reader in readers.values())

    partial_readers = reader_doubles()
    partial_readers.pop(ReviewConcern.PERFORMANCE)
    with pytest.raises(ReviewError, match="performance"):
        ReviewService(
            store, provider=provider, readers=partial_readers
        ).run_ready_review("PR-MISSING")

    with pytest.raises(ReviewError, match="provider"):
        ReviewService(store).run_ready_review("PR-READY")
    with pytest.raises(ReviewError, match="Unknown pull request"):
        service.run_ready_review("PR-UNKNOWN")
    provider.targets["PR-WRONG"] = PullRequestTarget("OTHER", "wrong-1")
    with pytest.raises(ReviewError, match="wrong pull request"):
        service.run_ready_review("PR-WRONG")

    provider.targets["PR-READY"] = PullRequestTarget("OTHER", "ready-1")
    with pytest.raises(ReviewError, match="wrong pull request"):
        service.start_cycle("PR-READY", "ready-1")
    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-1", ready=False)
    with pytest.raises(ReviewError, match="not ready"):
        service.start_cycle("PR-READY", "ready-1")
    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-2")
    with pytest.raises(ReviewError, match="does not match"):
        service.start_cycle("PR-READY", "ready-1")

    provider.targets["PR-READY"] = PullRequestTarget("PR-READY", "ready-1", ready=False)
    with pytest.raises(ReviewError, match="not ready"):
        service.run_ready_review("PR-READY")


def test_default_production_review_adapters_require_token_at_startup(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with (
        pytest.raises(RuntimeError, match="GITHUB_TOKEN or GH_TOKEN"),
        TestClient(
            create_app(
                review_store=review_store,
                require_review_adapters=True,
            )
        ),
    ):
        pass


def test_default_production_review_adapters_start_with_read_token(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.setenv("GITHUB_TOKEN", "test-read-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    app = create_app(
        review_store=review_store,
        api_key="test-key",
        review_actor="operator",
        require_review_adapters=True,
    )
    with TestClient(app):
        pass


def test_merge_provider_io_runs_outside_the_review_write_transaction(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = LockInspectingProvider(
        store, {"PR-LOCK": PullRequestTarget("PR-LOCK", "h1")}
    )
    service = ReviewService(store, provider=provider)
    cycle = service.start_cycle("PR-LOCK", "h1")
    pass_all(service, cycle.cycle.cycle_id)

    handoff = service.merge_handoff("PR-LOCK", "h1")

    assert handoff.cycle_id == cycle.cycle.cycle_id
    assert provider.in_transaction_during_provider_call is False


def test_review_authorization_and_lookup_errors_are_explicit(
    review_store: ReviewStore,
) -> None:
    authorizer = AllowListReviewAuthorizer()
    assert authorizer.authorize("PR", " ", "read") is False
    assert authorizer.authorize("PR", "reader", "reader") is True
    assert authorizer.authorize("PR", "reader", "writer") is False
    assert authorizer.authorize("PR", "writer", "handoff") is False

    store = review_store
    service = ReviewService(store)
    with pytest.raises(ReviewError, match="Unknown review cycle"):
        service.pull_request_id_for_cycle("missing")
    with pytest.raises(ReviewError, match="Unknown review finding"):
        service.pull_request_id_for_finding("missing")
    with pytest.raises(ReviewError, match="provider"):
        service.merge_handoff("PR-MISSING", "head-1")

    cycle = service.start_cycle("PR-NO-PROVIDER", "head-1")
    with pytest.raises(ReviewError, match="provider"):
        service.merge_handoff("PR-NO-PROVIDER", "head-2")
    assert cycle.cycle.pull_request_id == "PR-NO-PROVIDER"
    provider_store = ReviewStore()
    try:
        provider_service = ReviewService(
            provider_store,
            provider=FixtureProvider(
                {"PR-MISSING": PullRequestTarget("PR-MISSING", "head-1")}
            ),
        )
        with pytest.raises(ReviewError, match="No review cycle"):
            provider_service.merge_handoff("PR-MISSING", "head-1")
    finally:
        provider_store.close()


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
