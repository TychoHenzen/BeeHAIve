from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.routing import RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from tests.support.meta_review.helpers import seed_meta_review


def test_meta_review_store_reclaims_stale_review_and_enforces_global_overlap(
    tmp_path: Path,
) -> None:
    database = tmp_path / "meta-review.db"
    first = OrchestratorStore(database)
    second = OrchestratorStore(database)
    try:
        seed_meta_review(first)
        first.sync_project(
            ProjectSnapshot(
                "project-2",
                "Planning 2",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)
                    ),
                ),
            )
        )
        first.begin_meta_review("stale", "project-1", 1, 1)
        with first._transaction() as connection:
            connection.execute(
                "UPDATE meta_review_runs SET created_at = ? WHERE review_id = ?",
                (
                    (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "stale",
                ),
            )
        second.begin_meta_review("fresh", "project-2", 1, 1)
        with first._lock:
            stale = first._connection.execute(
                "SELECT status, error FROM meta_review_runs WHERE review_id = ?",
                ("stale",),
            ).fetchone()
        assert stale["status"] == "failed"
        assert stale["error"] == "Meta-review lease expired"
        with pytest.raises(StoreError, match="already running"):
            first.begin_meta_review("blocked", "project-1", 1, 1)
    finally:
        first.close()
        second.close()


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
