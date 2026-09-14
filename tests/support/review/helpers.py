from collections.abc import Iterator

import pytest

from beehaiive.review import (
    REQUIRED_CONCERNS,
    ReaderStatus,
    ReviewConcern,
    ReviewService,
    ReviewStore,
)
from tests.support.review.reader_double import ReaderDouble as ReaderDouble


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()


def reader_doubles(
    status: ReaderStatus = ReaderStatus.PASS,
) -> dict[ReviewConcern, ReaderDouble]:
    return {concern: ReaderDouble(status) for concern in REQUIRED_CONCERNS}


def pass_all(service: ReviewService, cycle_id: str) -> None:
    for concern in REQUIRED_CONCERNS:
        service.record_reader(cycle_id, concern, ReaderStatus.PASS)
