from __future__ import annotations

import json
from typing import Any

from ..provider import (
    ProviderError,
    complete_graphql_connection,
    require_graphql_mapping,
)
from ..review import (
    MAX_REVIEW_EVIDENCE_BYTES,
    PullRequestTarget,
)
from .github_helpers import optional_mapping, required_connection_nodes, required_fields
from .github_queries import (
    PULL_REQUEST_ID_MARKER,
    PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY,
    PULL_REQUEST_REVIEW_SNAPSHOT_QUERY,
    PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY,
    PULL_REQUEST_REVIEWS_PAGE_QUERY,
    PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY,
)


class GithubReadMixin:
    def get_pull_request(self: Any, pull_request_id: str) -> PullRequestTarget:
        match = PULL_REQUEST_ID_MARKER.fullmatch(pull_request_id)
        if match is None:
            raise ProviderError("Pull-request id must use owner/repository#number")
        owner = match.group("owner")
        repository = match.group("repository")
        number = int(match.group("number"))
        if number > 2_147_483_647:
            raise ProviderError("Pull-request number is outside the supported range")

        client = self._configured_client()
        variables = {"owner": owner, "name": repository, "number": number}
        data = client.execute(PULL_REQUEST_REVIEW_SNAPSHOT_QUERY, variables)
        repository_value: object = data.get("repository")
        if repository_value is None:
            raise ProviderError("GitHub GraphQL omitted repository data")
        repository_data = require_graphql_mapping(repository_value)
        pull_request_value: object = repository_data.get("pullRequest")
        if pull_request_value is None:
            raise ProviderError("GitHub pull request was not found")
        pull_request = require_graphql_mapping(pull_request_value)
        state = pull_request.get("state")
        merged = pull_request.get("merged")
        is_draft = pull_request.get("isDraft")
        if (
            not isinstance(state, str)
            or not isinstance(merged, bool)
            or not isinstance(is_draft, bool)
        ):
            raise ProviderError("GitHub pull request state is incomplete")
        head_ref = optional_mapping(pull_request.get("headRef"))
        head_target = optional_mapping(head_ref.get("target"))
        head_sha = head_target.get("oid")
        if state != "OPEN" or merged or is_draft:
            return PullRequestTarget(
                pull_request_id,
                head_sha if isinstance(head_sha, str) else "unavailable",
                ready=False,
            )
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise ProviderError("GitHub pull request omitted its current head SHA")
        required_fields(pull_request, ("id", "number", "url"), "pull request")
        pull_request_node_id = pull_request.get("id")
        pull_request_url = pull_request.get("url")
        if (
            not isinstance(pull_request_node_id, str)
            or not pull_request_node_id
            or pull_request.get("number") != number
            or not isinstance(pull_request_url, str)
            or not pull_request_url
        ):
            raise ProviderError("GitHub pull request identity is incomplete")

        reviews = complete_graphql_connection(
            client,
            pull_request.get("reviews"),
            PULL_REQUEST_REVIEWS_PAGE_QUERY,
            variables,
            ("repository", "pullRequest", "reviews"),
            strict=True,
        )
        requests = complete_graphql_connection(
            client,
            pull_request.get("reviewRequests"),
            PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY,
            variables,
            ("repository", "pullRequest", "reviewRequests"),
            strict=True,
        )
        threads = complete_graphql_connection(
            client,
            pull_request.get("reviewThreads"),
            PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY,
            variables,
            ("repository", "pullRequest", "reviewThreads"),
            strict=True,
        )

        review_nodes = required_connection_nodes(reviews, "reviews")
        request_nodes = required_connection_nodes(requests, "review requests")
        thread_nodes = required_connection_nodes(threads, "review threads")
        completed_threads: list[dict[str, object]] = []
        for raw_thread in thread_nodes:
            thread = dict(raw_thread)
            required_fields(
                thread,
                (
                    "id",
                    "path",
                    "line",
                    "originalLine",
                    "startLine",
                    "originalStartLine",
                    "diffSide",
                    "startDiffSide",
                    "isResolved",
                    "isOutdated",
                    "comments",
                ),
                "review thread",
            )
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise ProviderError("GitHub review thread omitted its identifier")
            comments = complete_graphql_connection(
                client,
                thread.get("comments"),
                PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY,
                {"threadId": thread_id},
                ("node", "comments"),
                strict=True,
            )
            comment_nodes = required_connection_nodes(comments, "thread comments")
            for comment in comment_nodes:
                required_fields(
                    comment,
                    ("id", "author", "body", "createdAt", "updatedAt", "url"),
                    "review comment",
                )
            thread["comments"] = comment_nodes
            completed_threads.append(thread)

        for review in review_nodes:
            required_fields(
                review,
                ("id", "author", "state", "body", "submittedAt", "url"),
                "review",
            )
        for request in request_nodes:
            required_fields(request, ("requestedReviewer",), "review request")

        evidence = {
            "pull_request": {
                "id": pull_request_node_id,
                "number": number,
                "repository": f"{owner}/{repository}",
                "url": pull_request_url,
                "state": state,
                "merged": merged,
                "isDraft": is_draft,
                "head_sha": head_sha,
            },
            "reviews": review_nodes,
            "requested_reviewers": request_nodes,
            "review_threads": completed_threads,
        }
        serialized = json.dumps(
            self._redact(evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) > MAX_REVIEW_EVIDENCE_BYTES:
            raise ProviderError("GitHub review evidence exceeds the storage limit")
        return PullRequestTarget(
            pull_request_id,
            head_sha,
            evidence_json=serialized,
        )
