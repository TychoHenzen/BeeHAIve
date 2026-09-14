from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    PullRequestTarget,
    ReviewService,
    ReviewStore,
)
from main import create_app
from tests.support.review.exploding_provider import (
    ExplodingProvider as ExplodingProvider,
)
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import reader_doubles


def test_review_api_persists_github_evidence_references(
    review_store: ReviewStore,
) -> None:
    target = PullRequestTarget(
        "owner/repo#8",
        "evidence-head",
        evidence_json=(
            '{"pull_request":{"id":"PULL_REQUEST_NODE"},'
            '"reviews":[{"id":"REVIEW_NODE"}]}'
        ),
    )
    service = ReviewService(
        review_store,
        provider=FixtureProvider({target.pull_request_id: target}),
        readers=reader_doubles(),
    )
    client = TestClient(
        create_app(review_service=service, api_key="test-key", review_actor="operator")
    )
    headers = {"X-API-Key": "test-key"}
    started = client.post(
        "/reviews/cycles",
        json={"pull_request_id": target.pull_request_id, "head_sha": target.head_sha},
        headers=headers,
    )
    assert started.status_code == 200
    cycle_id = started.json()["cycle"]["cycle_id"]
    assert started.json()["cycle"]["github_evidence"]["pull_request"]["id"] == (
        "PULL_REQUEST_NODE"
    )

    recorded = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "security",
            "status": "pass",
            "evidence_refs": ["PULL_REQUEST_NODE"],
        },
        headers=headers,
    )
    assert recorded.status_code == 200
    security = next(
        item for item in recorded.json()["readers"] if item["concern"] == "security"
    )
    assert security["evidence_refs"] == ["PULL_REQUEST_NODE"]

    finding = client.post(
        f"/reviews/cycles/{cycle_id}/findings",
        json={
            "concern": "test_coverage",
            "summary": "Coverage gap",
            "evidence_refs": ["REVIEW_NODE"],
        },
        headers=headers,
    )
    assert finding.status_code == 200
    assert finding.json()["findings"][0]["evidence_refs"] == ["REVIEW_NODE"]

    invalid = client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "performance",
            "status": "pass",
            "evidence_refs": ["NOT_IN_SNAPSHOT"],
        },
        headers=headers,
    )
    assert invalid.status_code == 409


def test_review_api_maps_provider_adapter_failures_to_dependency_errors(
    review_store: ReviewStore,
) -> None:
    store = review_store
    client = TestClient(
        create_app(
            review_store=store,
            review_provider=ExplodingProvider(),
            review_readers=reader_doubles(),
            api_key="test-key",
            review_actor="writer",
        )
    )

    response = client.post(
        "/reviews/ready",
        json={"pull_request_id": "PR-FAIL"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Pull-request provider failed"


def test_review_api_requires_a_configured_server_actor(
    review_store: ReviewStore,
) -> None:
    client = TestClient(create_app(review_store=review_store, api_key="test-key"))

    response = client.get(
        "/reviews/pull-requests/PR-ACTOR",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Review actor is not configured"


def test_create_app_rejects_conflicting_review_store() -> None:
    service_store = ReviewStore()
    api_store = ReviewStore()
    try:
        with pytest.raises(ValueError, match="share one review store"):
            create_app(
                review_service=ReviewService(service_store),
                review_store=api_store,
            )
    finally:
        service_store.close()
        api_store.close()


def test_create_app_rejects_adapters_with_existing_review_service(
    review_store: ReviewStore,
) -> None:
    store = review_store
    with pytest.raises(ValueError, match="adapters"):
        create_app(
            review_service=ReviewService(store),
            review_provider=FixtureProvider({}),
        )


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
