from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beehaiive.review_github import (
    PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY,
    PULL_REQUEST_REVIEW_SNAPSHOT_QUERY,
    PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY,
    PULL_REQUEST_REVIEWS_PAGE_QUERY,
    PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY,
)
from tests.support.provider.helpers import (
    build_comment,
    build_connection,
    build_review,
    build_thread,
)


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
            reviews = build_connection(
                [build_review("REVIEW-BOT", "Bot", "APPROVED")], True, "reviews-next"
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
                "reviewRequests": build_connection(
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
                "reviewThreads": build_connection(
                    [
                        build_thread(
                            "THREAD-1",
                            build_connection(
                                [
                                    build_comment(
                                        "COMMENT-1", "Bot", "Bearer unit-secret"
                                    )
                                ],
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
                        "reviews": build_connection(
                            [build_review("REVIEW-USER", "User", "CHANGES_REQUESTED")]
                        )
                    }
                }
            }
        if query == PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY:
            assert variables["cursor"] == "requests-next"
            return {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": build_connection(
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
                        "reviewThreads": build_connection(
                            [
                                build_thread(
                                    "THREAD-2",
                                    build_connection(
                                        [
                                            build_comment(
                                                "COMMENT-3", "User", "Second thread"
                                            )
                                        ]
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
                    "comments": build_connection(
                        [build_comment("COMMENT-2", "User", "More inline feedback")]
                    )
                }
            }
        raise AssertionError("Unexpected GitHub query")
