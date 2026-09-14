from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from beehaiive.review import (
    PullRequestTarget,
    ReviewStore,
)
from main import create_app
from tests.support.review.fixture_provider import FixtureProvider as FixtureProvider
from tests.support.review.helpers import reader_doubles


def test_review_api_runs_reader_cycle_finding_and_handoff_paths(
    review_store: ReviewStore,
) -> None:
    store = review_store
    provider = FixtureProvider({"PR-API": PullRequestTarget("PR-API", "h1")})

    def client_for(actor: str) -> TestClient:
        return TestClient(
            create_app(
                review_store=store,
                review_provider=provider,
                api_key="test-key",
                review_actor=actor,
            )
        )

    reader_client = client_for("reader")
    writer_client = client_for("writer")
    operator_client = client_for("operator")
    api_headers = {"X-API-Key": "test-key"}

    assert (
        writer_client.post(
            "/reviews/cycles",
            json={"pull_request_id": "PR-API", "head_sha": "h1"},
            headers=api_headers,
        ).status_code
        == 200
    )
    unauthorized = reader_client.get("/reviews/pull-requests/PR-API")
    assert unauthorized.status_code == 401
    started = reader_client.get("/reviews/pull-requests/PR-API", headers=api_headers)
    cycle_id = started.json()["cycle"]["cycle_id"]
    forged_actor = reader_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "forged"},
        headers={**api_headers, "X-Review-Actor": "operator"},
    )
    assert forged_actor.status_code == 409

    failed = reader_client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={
            "concern": "security",
            "status": "fail",
            "findings": ["Unsafe redirect"],
        },
        headers=api_headers,
    )
    assert failed.status_code == 200
    finding_id = failed.json()["findings"][0]["finding_id"]
    assert failed.json()["writer_feedback"][0]["finding_id"] == finding_id
    pending = reader_client.post(
        f"/reviews/cycles/{cycle_id}/readers",
        json={"concern": "test_coverage", "status": "pending"},
        headers=api_headers,
    )
    assert pending.status_code == 200
    assert pending.json()["readers"][1]["concern"] == "test_coverage"
    assert pending.json()["readers"][1]["status"] == "pending"
    added = writer_client.post(
        f"/reviews/cycles/{cycle_id}/findings",
        json={
            "concern": "clean_code",
            "summary": "Nested responsibility",
            "file_path": "src/review.py",
            "start_line": 4,
            "end_line": 6,
        },
        headers=api_headers,
    )
    assert added.status_code == 200
    assert added.json()["findings"][-1]["file_path"] == "src/review.py"
    assert added.json()["findings"][-1]["start_line"] == 4
    denied_publish = reader_client.post(
        f"/reviews/findings/{finding_id}/publish", headers=api_headers
    )
    assert denied_publish.status_code == 409
    retryable_publish = writer_client.post(
        f"/reviews/findings/{finding_id}/publish", headers=api_headers
    )
    assert retryable_publish.status_code == 200
    published_finding = next(
        item
        for item in retryable_publish.json()["findings"]
        if item["finding_id"] == finding_id
    )
    assert published_finding["publication"]["state"] == "retryable"
    resolved = writer_client.post(
        f"/reviews/findings/{finding_id}/resolve",
        json={"resolution": "Validated redirect target"},
        headers=api_headers,
    )
    assert resolved.status_code == 200
    blocked = operator_client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=api_headers,
    )
    assert blocked.status_code == 409

    denied_approval = reader_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=api_headers,
    )
    assert denied_approval.status_code == 409
    approved = operator_client.post(
        f"/reviews/cycles/{cycle_id}/approve",
        json={"reason": "Human accepted the remaining findings"},
        headers=api_headers,
    )
    handoff = operator_client.post(
        "/reviews/pull-requests/PR-API/handoff",
        json={"head_sha": "h1"},
        headers=api_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["cycle"]["human_approval"] is True
    assert approved.json()["cycle"]["approval_actor"] == "operator"
    assert approved.json()["cycle"]["approval_reason"] == (
        "Human accepted the remaining findings"
    )
    assert handoff.status_code == 409
    assert "owner/repository#number" in handoff.json()["detail"]


def test_review_ready_api_runs_reader_doubles(review_store: ReviewStore) -> None:
    store = review_store
    provider = FixtureProvider(
        {"PR-READY-API": PullRequestTarget("PR-READY-API", "api-ready-1")}
    )
    readers = reader_doubles()
    client = TestClient(
        create_app(
            review_store=store,
            review_provider=provider,
            review_readers=readers,
            api_key="test-key",
            review_actor="writer",
        )
    )

    response = client.post(
        "/reviews/ready",
        json={"pull_request_id": "PR-READY-API"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["cycle"]["status"] == "passed"
    assert all(reader.calls == 1 for reader in readers.values())


@pytest.fixture
def review_store() -> Iterator[ReviewStore]:
    store = ReviewStore()
    try:
        yield store
    finally:
        store.close()
