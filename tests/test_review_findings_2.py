import sqlite3
from collections.abc import Iterator

import pytest

from beehaiive.review import (
    PullRequestTarget,
    ReviewError,
    ReviewService,
    ReviewStore,
)
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import pass_all


def test_merge_handoff_rechecks_the_cycle_after_provider_io(
    review_store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FixtureProvider({"PR-RACE": PullRequestTarget("PR-RACE", "h1")})
    service = ReviewService(review_store, provider=provider)
    cycle = service.start_cycle("PR-RACE", "h1")
    pass_all(service, cycle.cycle.cycle_id)

    original_current_cycle_row = review_store.current_cycle_row
    calls = 0

    def disappearing_current_cycle(
        connection: sqlite3.Connection, pull_request_id: str
    ) -> sqlite3.Row | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_current_cycle_row(connection, pull_request_id)
        return None

    monkeypatch.setattr(review_store, "current_cycle_row", disappearing_current_cycle)
    with pytest.raises(ReviewError, match="No review cycle"):
        service.merge_handoff("PR-RACE", "h1")


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
