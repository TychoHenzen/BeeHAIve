from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from beehaiive.provider import ProviderError
from beehaiive.review import (
    REQUIRED_CONCERNS,
    ReviewCycleStatus,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_github import (
    PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY,
    PULL_REQUEST_REVIEW_SNAPSHOT_QUERY,
    PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY,
    PULL_REQUEST_REVIEWS_PAGE_QUERY,
    PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY,
    GitHubReviewProvider,
    github_review_readers,
)
from main import create_app


def _connection(
    nodes: list[dict[str, Any]], has_next: bool = False, cursor: str | None = None
) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


def _review(identifier: str, actor_type: str, state: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "author": {
            "__typename": actor_type,
            "id": f"{identifier}-author",
            "login": f"{actor_type.lower()}-reviewer",
        },
        "state": state,
        "body": f"Review body {identifier}",
        "submittedAt": "2026-09-13T10:00:00Z",
        "url": f"https://github.com/owner/repo/pull/7#pullrequestreview-{identifier}",
    }


def _comment(identifier: str, actor_type: str, body: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "author": {
            "__typename": actor_type,
            "id": f"{identifier}-author",
            "login": f"{actor_type.lower()}-commenter",
        },
        "body": body,
        "createdAt": "2026-09-13T10:01:00Z",
        "updatedAt": "2026-09-13T10:02:00Z",
        "url": f"https://github.com/owner/repo/pull/7#discussion-{identifier}",
    }


def _thread(identifier: str, comments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": identifier,
        "path": "src/app.py",
        "line": 22,
        "originalLine": 20,
        "startLine": 18,
        "originalStartLine": 16,
        "diffSide": "RIGHT",
        "startDiffSide": "RIGHT",
        "isResolved": identifier == "THREAD-2",
        "isOutdated": identifier == "THREAD-1",
        "comments": comments,
    }


class ReviewGraphQLClient:
    def __init__(
        self,
        *,
        state: str = "OPEN",
        merged: bool = False,
        is_draft: bool = False,
        malformed_reviews: bool = False,
    ) -> None:
        self.state = state
        self.merged = merged
        self.is_draft = is_draft
        self.malformed_reviews = malformed_reviews
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, query: str, variables: Mapping[str, object]) -> dict[str, Any]:
        self.calls.append((query, dict(variables)))
        if query == PULL_REQUEST_REVIEW_SNAPSHOT_QUERY:
            reviews = _connection(
                [_review("REVIEW-BOT", "Bot", "APPROVED")], True, "reviews-next"
            )
            if self.malformed_reviews:
                del reviews["pageInfo"]
            pull_request = {
                "id": "PULL_REQUEST_NODE",
                "number": 7,
                "url": "https://github.com/owner/repo/pull/7",
                "state": self.state,
                "merged": self.merged,
                "isDraft": self.is_draft,
                "headRef": {"target": {"oid": "head-7"}},
                "reviews": reviews,
                "reviewRequests": _connection(
                    [
                        {
                            "requestedReviewer": {
                                "__typename": "User",
                                "id": "USER-1",
                                "login": "reviewer-one",
                                "name": "Reviewer One",
                            }
                        }
                    ],
                    True,
                    "requests-next",
                ),
                "reviewThreads": _connection(
                    [
                        _thread(
                            "THREAD-1",
                            _connection(
                                [_comment("COMMENT-1", "Bot", "Bearer unit-secret")],
                                True,
                                "comments-next",
                            ),
                        )
                    ],
                    True,
                    "threads-next",
                ),
            }
            return {"repository": {"pullRequest": pull_request}}
        if query == PULL_REQUEST_REVIEWS_PAGE_QUERY:
            assert variables["cursor"] == "reviews-next"
            return {
                "repository": {
                    "pullRequest": {
                        "reviews": _connection(
                            [_review("REVIEW-USER", "User", "CHANGES_REQUESTED")]
                        )
                    }
                }
            }
        if query == PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY:
            assert variables["cursor"] == "requests-next"
            return {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": _connection(
                            [
                                {
                                    "requestedReviewer": {
                                        "__typename": "Team",
                                        "id": "TEAM-1",
                                        "name": "Security",
                                        "slug": "security",
                                    }
                                }
                            ]
                        )
                    }
                }
            }
        if query == PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY:
            assert variables["cursor"] == "threads-next"
            return {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": _connection(
                            [
                                _thread(
                                    "THREAD-2",
                                    _connection(
                                        [_comment("COMMENT-3", "User", "Second thread")]
                                    ),
                                )
                            ]
                        )
                    }
                }
            }
        if query == PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY:
            assert variables["threadId"] == "THREAD-1"
            assert variables["cursor"] == "comments-next"
            return {
                "node": {
                    "comments": _connection(
                        [_comment("COMMENT-2", "User", "More inline feedback")]
                    )
                }
            }
        raise AssertionError("Unexpected GitHub query")


def test_github_review_provider_reads_and_retains_every_review_page() -> None:
    client = ReviewGraphQLClient()
    target = GitHubReviewProvider(token="unit-secret", client=client).get_pull_request(
        "owner/repo#7"
    )

    assert target.ready
    assert target.head_sha == "head-7"
    assert target.evidence_json is not None
    assert "unit-secret" not in target.evidence_json
    evidence = json.loads(target.evidence_json)
    assert evidence["pull_request"]["id"] == "PULL_REQUEST_NODE"
    assert [review["id"] for review in evidence["reviews"]] == [
        "REVIEW-BOT",
        "REVIEW-USER",
    ]
    assert evidence["reviews"][0]["author"]["__typename"] == "Bot"
    assert evidence["reviews"][1]["state"] == "CHANGES_REQUESTED"
    assert [
        request["requestedReviewer"]["__typename"]
        for request in evidence["requested_reviewers"]
    ] == ["User", "Team"]
    threads = evidence["review_threads"]
    assert [thread["id"] for thread in threads] == ["THREAD-1", "THREAD-2"]
    assert threads[0]["path"] == "src/app.py"
    assert threads[0]["startLine"] == 18
    assert threads[0]["diffSide"] == "RIGHT"
    assert threads[0]["isOutdated"] is True
    assert threads[1]["isResolved"] is True
    assert [comment["id"] for comment in threads[0]["comments"]] == [
        "COMMENT-1",
        "COMMENT-2",
    ]
    assert threads[0]["comments"][0]["body"] == "Bearer [redacted]"
    assert len(client.calls) == 5
    assert all("mutation" not in query.lower() for query, _ in client.calls)


@pytest.mark.parametrize(
    "pull_request_id",
    (
        "repo#7",
        "owner/repo",
        "owner/repo#0",
        "owner/repo/sub#7",
        "owner/repo#2147483648",
    ),
)
def test_github_review_provider_rejects_unqualified_or_invalid_ids(
    pull_request_id: str,
) -> None:
    client = ReviewGraphQLClient()
    with pytest.raises(ProviderError):
        GitHubReviewProvider(client=client).get_pull_request(pull_request_id)
    assert client.calls == []


@pytest.mark.parametrize(
    ("state", "merged", "is_draft"),
    (("CLOSED", False, False), ("OPEN", False, True), ("CLOSED", True, False)),
)
def test_github_review_provider_does_not_read_nonready_pull_requests(
    state: str, merged: bool, is_draft: bool
) -> None:
    client = ReviewGraphQLClient(state=state, merged=merged, is_draft=is_draft)
    target = GitHubReviewProvider(client=client).get_pull_request("owner/repo#7")
    assert not target.ready
    assert target.evidence_json is None
    assert len(client.calls) == 1


def test_github_review_provider_rejects_incomplete_pagination() -> None:
    client = ReviewGraphQLClient(malformed_reviews=True)
    with pytest.raises(ProviderError, match="pagination state"):
        GitHubReviewProvider(client=client).get_pull_request("owner/repo#7")


def test_production_readers_persist_evidence_and_remain_pending_without_policy() -> (
    None
):
    client = ReviewGraphQLClient()
    provider = GitHubReviewProvider(token="unit-secret", client=client)
    store = ReviewStore()
    try:
        service = ReviewService(
            store, provider=provider, readers=github_review_readers()
        )
        first = service.run_ready_review("owner/repo#7")
        second = service.run_ready_review("owner/repo#7")

        assert first.cycle.status is ReviewCycleStatus.ACTIVE
        assert second.cycle.cycle_id == first.cycle.cycle_id
        assert len(second.readers) == len(REQUIRED_CONCERNS) == 4
        assert all(reader.status.value == "pending" for reader in second.readers)
        assert all(
            reader.evidence_refs == ("PULL_REQUEST_NODE",) for reader in second.readers
        )
        response = second.as_dict()
        assert response["cycle"]["github_evidence"]["pull_request"]["id"] == (
            "PULL_REQUEST_NODE"
        )
        assert response["findings"] == []
    finally:
        store.close()


def test_production_adapters_work_through_review_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "production")
    monkeypatch.setenv("BEEHAIIVE_REVIEW_ACTOR", "operator")
    store = ReviewStore()
    pull_request_id = "owner/repo#7"
    try:
        app = create_app(
            review_store=store,
            review_provider=GitHubReviewProvider(client=ReviewGraphQLClient()),
            review_readers=github_review_readers(),
            api_key="review-api-key",
            require_review_adapters=True,
        )
        with TestClient(app) as client:
            ready = client.post(
                "/reviews/ready",
                json={"pull_request_id": pull_request_id},
                headers={"X-API-Key": "review-api-key"},
            )
            assert ready.status_code == 200, ready.text
            assert all(
                reader["status"] == "pending" for reader in ready.json()["readers"]
            )

            snapshot = client.get(
                f"/reviews/pull-requests/{quote(pull_request_id, safe='')}",
                headers={"X-API-Key": "review-api-key"},
            )
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["cycle"]["pull_request_id"] == pull_request_id
            assert snapshot.json()["cycle"]["github_evidence"]["pull_request"][
                "id"
            ] == ("PULL_REQUEST_NODE")
    finally:
        store.close()
