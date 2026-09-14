from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beehaiive.review_github import (
    PULL_REQUEST_REVIEW_SNAPSHOT_QUERY,
)
from tests.support.provider.helpers import (
    build_comment,
    build_connection,
    build_review,
    build_thread,
)


class PublishingGraphQLClient:
    def __init__(self, head_sha: str = "head-7") -> None:
        self.head_sha = head_sha
        self.reviews: list[dict[str, Any]] = []
        self.threads: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []

    def execute(self, query: str, variables: Mapping[str, object]) -> dict[str, Any]:
        if "mutation" in query.lower():
            raw_input = variables["input"]
            assert isinstance(raw_input, Mapping)
            mutation_input = dict(raw_input)
            self.mutations.append(mutation_input)
            review_number = len(self.mutations)
            review_id = f"REVIEW-PUBLISHED-{review_number}"
            review_url = f"https://github.com/owner/repo/pull/7#pullrequestreview-{review_number}"
            review = build_review(review_id, "User", "COMMENTED")
            review["body"] = mutation_input.get("body") or ""
            review["url"] = review_url
            self.reviews.append(review)
            raw_threads = mutation_input.get("threads", [])
            assert isinstance(raw_threads, list)
            for index, raw_thread_input in enumerate(raw_threads, start=1):
                assert isinstance(raw_thread_input, Mapping)
                thread_input = dict(raw_thread_input)
                thread_id = f"THREAD-PUBLISHED-{review_number}-{index}"
                comment = build_comment(
                    f"COMMENT-PUBLISHED-{review_number}-{index}",
                    "User",
                    str(thread_input["body"]),
                )
                comment["url"] = (
                    f"https://github.com/owner/repo/pull/7#discussion-{thread_id}"
                )
                thread = build_thread(thread_id, build_connection([comment]))
                thread.update(
                    {
                        "path": thread_input["path"],
                        "line": thread_input["line"],
                        "originalLine": thread_input["line"],
                        "startLine": thread_input.get("startLine"),
                        "originalStartLine": thread_input.get("startLine"),
                        "diffSide": thread_input["side"],
                        "startDiffSide": thread_input.get("startSide"),
                    }
                )
                self.threads.append(thread)
            return {
                "addPullRequestReview": {
                    "pullRequestReview": {"id": review_id, "url": review_url}
                }
            }
        if query == PULL_REQUEST_REVIEW_SNAPSHOT_QUERY:
            return {
                "repository": {
                    "pullRequest": {
                        "id": "PULL_REQUEST_NODE",
                        "number": 7,
                        "url": "https://github.com/owner/repo/pull/7",
                        "state": "OPEN",
                        "merged": False,
                        "isDraft": False,
                        "headRef": {"target": {"oid": self.head_sha}},
                        "reviews": build_connection(self.reviews),
                        "reviewRequests": build_connection([]),
                        "reviewThreads": build_connection(self.threads),
                    }
                }
            }
        raise AssertionError("Unexpected GitHub query")
