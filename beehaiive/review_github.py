from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from threading import Lock
from typing import Any, cast

from .provider import (
    GraphQLClient,
    ProviderError,
    UrllibGraphQLClient,
    complete_graphql_connection,
    require_graphql_mapping,
)
from .review import (
    MAX_REVIEW_EVIDENCE_BYTES,
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
    ReviewConcern,
)

PULL_REQUEST_REVIEW_SNAPSHOT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      url
      state
      merged
      isDraft
      headRef { target { oid } }
      reviews(first: 100) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
          state
          body
          submittedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { id login name }
            ... on Team { id name slug }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          startDiffSide
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              id
              author { __typename ... on User { id login } ... on Bot { id login } }
              body
              createdAt
              updatedAt
              url
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_REVIEWS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
          state
          body
          submittedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100, after: $cursor) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { id login name }
            ... on Team { id name slug }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          startDiffSide
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              id
              author { __typename ... on User { id login } ... on Bot { id login } }
              body
              createdAt
              updatedAt
              url
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY = """
query($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 20, after: $cursor) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
          body
          createdAt
          updatedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_PULL_REQUEST_ID = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repository>[A-Za-z0-9_.-]{1,100})#(?P<number>[1-9][0-9]{0,9})"
)


def _required_fields(
    value: Mapping[str, Any], fields: tuple[str, ...], label: str
) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ProviderError(f"GitHub review {label} omitted required fields")


def _required_connection_nodes(value: object, label: str) -> list[Mapping[str, Any]]:
    connection = require_graphql_mapping(value)
    raw_nodes = connection.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ProviderError(f"GitHub review {label} returned invalid nodes")
    nodes = cast(list[object], raw_nodes)
    if not all(isinstance(node, Mapping) for node in nodes):
        raise ProviderError(f"GitHub review {label} returned invalid nodes")
    return [cast(Mapping[str, Any], node) for node in nodes]


def _optional_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, Any], value)


class GitHubReviewProvider:
    """Read a complete, bounded GitHub review snapshot without GitHub writes."""

    def __init__(
        self,
        token: str | None = None,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._client = client
        self._endpoint = endpoint
        self._lock = Lock()

    def _token_from_environment(self) -> str | None:
        return (
            self._token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        )

    def validate_configuration(self) -> None:
        if self._client is None and not self._token_from_environment():
            raise ProviderError(
                "Set GITHUB_TOKEN or GH_TOKEN for production pull-request reviews"
            )

    def _configured_client(self) -> GraphQLClient:
        with self._lock:
            if self._client is None:
                token = self._token_from_environment()
                if not token:
                    raise ProviderError(
                        "Set GITHUB_TOKEN or GH_TOKEN for production "
                        "pull-request reviews"
                    )
                self._token = token
                self._client = UrllibGraphQLClient(token, self._endpoint)
            return self._client

    def _redact(self, value: object) -> object:
        if isinstance(value, str):
            from .agent import redact_worker_text

            secrets = tuple(
                secret
                for secret in (
                    self._token,
                    os.environ.get("GITHUB_TOKEN"),
                    os.environ.get("GH_TOKEN"),
                )
                if secret
            )
            return redact_worker_text(value, secrets, max_length=None)
        if isinstance(value, Mapping):
            mapping = cast(Mapping[str, object], value)
            return {key: self._redact(nested) for key, nested in mapping.items()}
        if isinstance(value, list):
            return [self._redact(nested) for nested in cast(list[object], value)]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise ProviderError("GitHub review evidence contains an unsupported value")

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        match = _PULL_REQUEST_ID.fullmatch(pull_request_id)
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
        head_ref = _optional_mapping(pull_request.get("headRef"))
        head_target = _optional_mapping(head_ref.get("target"))
        head_sha = head_target.get("oid")
        if state != "OPEN" or merged or is_draft:
            return PullRequestTarget(
                pull_request_id,
                head_sha if isinstance(head_sha, str) else "unavailable",
                ready=False,
            )
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise ProviderError("GitHub pull request omitted its current head SHA")
        _required_fields(pull_request, ("id", "number", "url"), "pull request")
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

        review_nodes = _required_connection_nodes(reviews, "reviews")
        request_nodes = _required_connection_nodes(requests, "review requests")
        thread_nodes = _required_connection_nodes(threads, "review threads")
        completed_threads: list[dict[str, object]] = []
        for raw_thread in thread_nodes:
            thread = dict(raw_thread)
            _required_fields(
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
            comment_nodes = _required_connection_nodes(comments, "thread comments")
            for comment in comment_nodes:
                _required_fields(
                    comment,
                    ("id", "author", "body", "createdAt", "updatedAt", "url"),
                    "review comment",
                )
            thread["comments"] = comment_nodes
            completed_threads.append(thread)

        for review in review_nodes:
            _required_fields(
                review,
                ("id", "author", "state", "body", "submittedAt", "url"),
                "review",
            )
        for request in request_nodes:
            _required_fields(request, ("requestedReviewer",), "review request")

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


class GitHubEvidenceReviewReader:
    """Leave verdicts pending until concern-specific analysis is configured."""

    def __init__(self, concern: ReviewConcern) -> None:
        self.concern = concern

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        if target.evidence_json is None:
            return ReaderExecution(
                ReaderStatus.FAIL,
                (f"GitHub review evidence is missing for {self.concern.value}",),
            )
        evidence_value: object = json.loads(target.evidence_json)
        pull_request: object = (
            cast(dict[str, object], evidence_value).get("pull_request")
            if isinstance(evidence_value, dict)
            else None
        )
        identifier = (
            cast(dict[str, object], pull_request).get("id")
            if isinstance(pull_request, dict)
            else None
        )
        if not isinstance(identifier, str) or not identifier:
            message = (
                f"GitHub pull-request evidence is incomplete for {self.concern.value}"
            )
            return ReaderExecution(
                ReaderStatus.FAIL,
                (message,),
            )
        return ReaderExecution(ReaderStatus.PENDING, evidence_refs=(identifier,))


def github_review_readers() -> dict[ReviewConcern, GitHubEvidenceReviewReader]:
    return {
        ReviewConcern.SECURITY: GitHubEvidenceReviewReader(ReviewConcern.SECURITY),
        ReviewConcern.TEST_COVERAGE: GitHubEvidenceReviewReader(
            ReviewConcern.TEST_COVERAGE
        ),
        ReviewConcern.CLEAN_CODE: GitHubEvidenceReviewReader(ReviewConcern.CLEAN_CODE),
        ReviewConcern.PERFORMANCE: GitHubEvidenceReviewReader(
            ReviewConcern.PERFORMANCE
        ),
    }
