import json
from collections.abc import Iterator

import pytest

from beehaiive.meta_review import (
    MetaReviewError,
    MetaReviewService,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.pbi_creation import PbiCreationRequest
from beehaiive.routing import RoutingStore
from beehaiive.storage import (
    MAX_META_REVIEW_EVENT_DETAILS_LENGTH,
    OrchestratorStore,
    StoreError,
)
from tests.support.meta_review.helpers import (
    complete_meta_review,
    seed_meta_review,
)


@pytest.mark.parametrize(
    "evidence_case",
    ["missing", "no-run", "malformed", "foreign-project", "mixed-repositories"],
)
def test_accept_rejects_unresolved_or_conflicting_run_evidence(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore], evidence_case: str
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store)
    local_run = complete_meta_review(store)
    if evidence_case == "missing":
        evidence_refs = ["run:missing"]
    elif evidence_case == "no-run":
        evidence_refs = ["artifact:review-1"]
    elif evidence_case == "malformed":
        evidence_refs = [f"run:{local_run}:unknown:4"]
    elif evidence_case == "foreign-project":
        store.sync_project(
            ProjectSnapshot(
                "project-2",
                "Planning 2",
                (
                    RepositorySnapshot(
                        "other/api", (PbiSnapshot("other/api", 1, "Other"),)
                    ),
                ),
            )
        )
        foreign_run = complete_meta_review(
            store, project_id="project-2", repository="other/api"
        )
        evidence_refs = [f"run:{foreign_run}"]
    else:
        store.sync_project(
            ProjectSnapshot(
                "project-1",
                "Planning",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/api", 1, "PBI 1"),)
                    ),
                    RepositorySnapshot(
                        "other/api", (PbiSnapshot("other/api", 2, "PBI 2"),)
                    ),
                ),
            )
        )
        other_run = complete_meta_review(store, 2, repository="other/api")
        evidence_refs = [f"run:{local_run}", f"run:{other_run}"]
    calls: list[tuple[PbiCreationRequest, str]] = []

    def create_pbi(request: PbiCreationRequest, key: str) -> dict[str, object]:
        calls.append((request, key))
        return {"status": "complete"}

    service = MetaReviewService(
        store,
        routing,
        lambda _records: [
            {
                "suggestion_key": evidence_case,
                "proposed_outcome": "Reject unsafe evidence",
                "rationale": "Source runs must agree.",
                "evidence_refs": evidence_refs,
            }
        ],
    )
    suggestion = service.run("project-1")["suggestions"][0]

    with pytest.raises(MetaReviewError):
        service.decide(
            "project-1",
            str(suggestion["suggestion_id"]),
            "accept",
            pbi_creator=create_pbi,
        )
    assert calls == []


def test_meta_review_store_boundaries_and_decision(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, _routing = meta_review_stores
    seed_meta_review(store)
    run_id = complete_meta_review(store)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE events SET details_json = ? WHERE run_id = ?",
            (
                json.dumps({"value": "x" * (MAX_META_REVIEW_EVENT_DETAILS_LENGTH + 1)}),
                run_id,
            ),
        )
    records = store.completed_session_records("project-1", None, 1)
    assert records[0]["events"]
    assert records[0]["events"][0]["details"] == {}
    with pytest.raises(StoreError, match="project id"):
        store.completed_session_records("", None, 1)
    with pytest.raises(StoreError, match="record_limit"):
        store.completed_session_records("project-1", None, 0)
    with pytest.raises(StoreError, match="id and project"):
        store.begin_meta_review("", "project-1", 1, 1)
    with pytest.raises(StoreError, match="Unknown project"):
        store.begin_meta_review("review-missing", "missing", 1, 1)
    with pytest.raises(StoreError, match="Unknown meta-review"):
        store.finish_meta_review("missing", "completed", 0, 0, [])
    with pytest.raises(StoreError, match="Unknown meta-review"):
        store.save_meta_review_suggestions("missing", [])
    with pytest.raises(StoreError, match="project id"):
        store.meta_review_suggestions("", None)
    with pytest.raises(StoreError, match="Invalid suggestion status"):
        store.meta_review_suggestions("project-1", "hold")
    with pytest.raises(StoreError, match="Invalid suggestion decision"):
        store.decide_meta_review_suggestion("project-1", "missing", "pending")
    with pytest.raises(StoreError, match="Unknown meta-review suggestion"):
        store.decide_meta_review_suggestion("project-1", "missing", "accepted")

    store.begin_meta_review("review-1", "project-1", 1, 1)
    with pytest.raises(StoreError, match="already running"):
        store.begin_meta_review("review-2", "project-1", 1, 1)
    with pytest.raises(StoreError, match="Invalid meta-review status"):
        store.finish_meta_review("review-1", "running", 0, 0, [])
    saved = store.save_meta_review_suggestions(
        "review-1",
        [
            {
                "suggestion_id": "suggestion-1",
                "suggestion_key": "key-1",
                "proposed_outcome": "Outcome",
                "rationale": "Reason",
                "evidence_refs": ["run:1"],
            }
        ],
    )
    assert saved[0]["status"] == "pending"
    assert (
        store.finish_meta_review("review-1", "completed", 0, 0, ["missing"])["status"]
        == "completed"
    )
    with pytest.raises(StoreError, match="not running"):
        store.complete_meta_review("review-1", 0, 0, [], [])
    decision = store.decide_meta_review_suggestion(
        "project-1", "suggestion-1", "rejected"
    )
    assert decision["status"] == "rejected"
    assert store.meta_review_suggestions("project-1", "rejected") == [decision]


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
