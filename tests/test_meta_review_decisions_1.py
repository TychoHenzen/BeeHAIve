from collections.abc import Iterator

import pytest

from beehaiive.meta_review import (
    MetaReviewError,
    MetaReviewService,
)
from beehaiive.pbi_creation import PbiCreationRequest
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
)
from tests.support.meta_review.helpers import complete_meta_review, seed_meta_review


def test_review_redacts_bounds_and_preserves_operator_decision(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store, count=2)
    first_run = complete_meta_review(
        store, 1, result="token=worker-secret " + "x" * 2_000
    )
    second_run = complete_meta_review(store, 2)
    router = ModelRouter(routing)
    for run_id in (first_run, second_run):
        router.begin(run_id)
        router.record(run_id, "failure", failure_context="fixture failure")
    captured: list[dict[str, object]] = []

    def analyzer(records: list[dict[str, object]]) -> list[dict[str, object]]:
        captured.extend(records)
        return [
            {
                "suggestion_key": "meta-review:v1:owner/api:routing-failure",
                "proposed_outcome": "Document the bounded workflow.",
                "rationale": "The completed run is reviewable.",
                "evidence_refs": [
                    f"run:{first_run}:attempt:1",
                    f"run:{second_run}:attempt:1",
                ],
            },
            {
                "suggestion_key": "meta-review:v1:owner/api:routing-failure",
                "proposed_outcome": "Duplicate",
                "rationale": "Duplicate",
                "evidence_refs": [f"run:{first_run}:attempt:1"],
            },
        ]

    pbi_creations: list[tuple[PbiCreationRequest, str]] = []

    def create_pbi(request: PbiCreationRequest, key: str) -> dict[str, object]:
        pbi_creations.append((request, key))
        return {"status": "complete", "issue": {"number": 10}}

    service = MetaReviewService(store, routing, analyzer)
    first = service.run("project-1")
    suggestion = first["suggestions"][0]
    assert "worker-secret" not in str(captured[0]["result"])
    assert len(str(captured[0]["result"])) <= 500
    assert len(service.suggestions("project-1")) == 1

    decided = service.decide(
        "project-1",
        str(suggestion["suggestion_id"]),
        "accept",
        pbi_creator=create_pbi,
    )
    assert decided["suggestion"]["status"] == "accepted"
    assert decided["pbi_creation"]["status"] == "complete"
    assert len(pbi_creations) == 1
    second = service.run("project-1")

    assert second["suggestions"][0]["suggestion_id"] == suggestion["suggestion_id"]
    assert service.suggestions("project-1", "accepted")[0]["status"] == "accepted"
    later = service.run("project-1", since="2999-01-01T00:00:00+00:00")
    assert later["suggestions"] == []
    assert service.suggestions("project-1", "accepted")[0]["status"] == "accepted"


def test_accept_creates_bounded_pbi_and_reuses_suggestion_key(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore],
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store, count=2)
    first_run = complete_meta_review(store, 1)
    second_run = complete_meta_review(store, 2)
    router = ModelRouter(routing)
    for run_id in (first_run, second_run):
        router.begin(run_id)
        router.record(run_id, "failure", failure_context="fixture failure")
    evidence_refs = [
        f"run:{first_run}:attempt:1",
        f"run:{second_run}:attempt:1",
        "artifact:review-1",
    ]
    calls: list[tuple[PbiCreationRequest, str]] = []
    incomplete = {"status": "outcome_unknown", "failure": {"code": "outcome_unknown"}}

    def create_pbi(request: PbiCreationRequest, key: str) -> dict[str, object]:
        calls.append((request, key))
        return incomplete

    service = MetaReviewService(
        store,
        routing,
        lambda _records: [
            {
                "suggestion_key": "accept-me",
                "proposed_outcome": "Create the accepted PBI",
                "rationale": "The evidence supports this change.",
                "evidence_refs": evidence_refs,
            }
        ],
    )
    suggestion = service.run("project-1")["suggestions"][0]
    suggestion_id = str(suggestion["suggestion_id"])
    qualifying_refs = [
        str(reference)
        for reference in suggestion["evidence_refs"]
        if ":attempt:" in str(reference)
    ]

    first = service.decide("project-1", suggestion_id, "accept", pbi_creator=create_pbi)
    second = service.decide(
        "project-1", suggestion_id, "accept", pbi_creator=create_pbi
    )

    assert first["suggestion"]["status"] == "accepted"
    assert first["pbi_creation"] == incomplete
    assert second["pbi_creation"] == incomplete
    assert len(calls) == 2
    first_request, first_key = calls[0]
    second_request, second_key = calls[1]
    assert first_request == second_request
    assert first_request.repository == "owner/api"
    assert first_request.title == "Create the accepted PBI"
    assert first_request.labels == ()
    assert "The evidence supports this change." in first_request.body
    assert any(first_run in reference for reference in qualifying_refs)
    assert any(second_run in reference for reference in qualifying_refs)
    assert all(reference in first_request.body for reference in qualifying_refs)
    assert "artifact:review-1" not in first_request.body
    assert "Project ID: project-1" in first_request.body
    assert f"Suggestion ID: {suggestion_id}" in first_request.body
    assert first_key == second_key == f"meta-review:project-1:{suggestion_id}"


@pytest.mark.parametrize("decision", ["reject", "pending"])
def test_non_accept_decisions_never_call_pbi_creator(
    meta_review_stores: tuple[OrchestratorStore, RoutingStore], decision: str
) -> None:
    store, routing = meta_review_stores
    seed_meta_review(store, count=2)
    first_run = complete_meta_review(store, 1)
    second_run = complete_meta_review(store, 2)
    router = ModelRouter(routing)
    for run_id in (first_run, second_run):
        router.begin(run_id)
        router.record(run_id, "failure", failure_context="fixture failure")
    calls: list[tuple[PbiCreationRequest, str]] = []

    def create_pbi(request: PbiCreationRequest, key: str) -> dict[str, object]:
        calls.append((request, key))
        return {"status": "complete"}

    service = MetaReviewService(
        store,
        routing,
        lambda _records: [
            {
                "suggestion_key": "do-not-create",
                "proposed_outcome": "No PBI",
                "rationale": "Not accepted.",
                "evidence_refs": [
                    f"run:{first_run}:attempt:1",
                    f"run:{second_run}:attempt:1",
                ],
            }
        ],
    )
    suggestion = service.run("project-1")["suggestions"][0]

    if decision == "pending":
        with pytest.raises(MetaReviewError, match="Decision"):
            service.decide(
                "project-1",
                str(suggestion["suggestion_id"]),
                decision,
                pbi_creator=create_pbi,
            )
        assert service.suggestions("project-1")[0]["status"] == "pending"
    else:
        result = service.decide(
            "project-1",
            str(suggestion["suggestion_id"]),
            decision,
            pbi_creator=create_pbi,
        )
        assert result["suggestion"]["status"] == "rejected"
    assert calls == []


@pytest.fixture
def meta_review_stores() -> Iterator[tuple[OrchestratorStore, RoutingStore]]:
    store = OrchestratorStore()
    routing = RoutingStore()
    yield store, routing
    store.close()
    routing.close()
