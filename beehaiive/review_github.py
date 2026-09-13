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
    FindingPublicationChannel,
    FindingPublicationState,
    PublicationOutcome,
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
    ReviewConcern,
    finding_publication_marker,
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

PUBLISH_PULL_REQUEST_REVIEW_MUTATION = """
mutation($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) {
    pullRequestReview { id url }
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


def _review_evidence(evidence_json: str | None) -> Mapping[str, Any]:
    if evidence_json is None:
        raise ProviderError("GitHub pull request omitted review evidence")
    try:
        evidence = json.loads(evidence_json)
    except json.JSONDecodeError as exc:
        raise ProviderError("GitHub review evidence is invalid") from exc
    if not isinstance(evidence, Mapping):
        raise ProviderError("GitHub review evidence is invalid")
    return cast(Mapping[str, Any], evidence)


def _pull_request_node_id(evidence_json: str | None) -> str:
    pull_request = _optional_mapping(
        _review_evidence(evidence_json).get("pull_request")
    )
    identifier = pull_request.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ProviderError("GitHub review evidence omitted its pull-request id")
    return identifier


def _publication_from_evidence(
    evidence_json: str | None,
    marker: str,
    expected_channel: FindingPublicationChannel,
    remote_id: str | None,
    remote_url: str | None,
) -> PublicationOutcome | None:
    evidence = _review_evidence(evidence_json)
    matches: list[tuple[FindingPublicationChannel, str, str]] = []
    for raw_review in evidence.get("reviews", []):
        if not isinstance(raw_review, Mapping):
            continue
        review = cast(Mapping[str, Any], raw_review)
        identifier, url, body = review.get("id"), review.get("url"), review.get("body")
        if (
            isinstance(identifier, str)
            and isinstance(url, str)
            and isinstance(body, str)
            and marker in body
        ):
            matches.append((FindingPublicationChannel.REVIEW_BODY, identifier, url))

    for raw_thread in evidence.get("review_threads", []):
        if not isinstance(raw_thread, Mapping):
            continue
        thread = cast(Mapping[str, Any], raw_thread)
        thread_id = thread.get("id")
        comments = thread.get("comments", [])
        if not isinstance(thread_id, str) or not isinstance(comments, list):
            continue
        for raw_comment in cast(list[object], comments):
            if not isinstance(raw_comment, Mapping):
                continue
            comment = cast(Mapping[str, Any], raw_comment)
            body, url = comment.get("body"), comment.get("url")
            if isinstance(body, str) and marker in body:
                matches.append(
                    (
                        FindingPublicationChannel.REVIEW_THREAD,
                        thread_id,
                        url if isinstance(url, str) else "",
                    )
                )

    if len(matches) > 1:
        return PublicationOutcome(
            FindingPublicationState.DUPLICATE_REMOTE,
            expected_channel,
            remote_id,
            remote_url,
            {
                "reason": "multiple_remote_markers",
                "remote_ids": [match[1] for match in matches[:5]],
            },
        )
    if matches:
        actual_channel, actual_id, actual_url = matches[0]
        if actual_channel is not expected_channel:
            return PublicationOutcome(
                FindingPublicationState.DUPLICATE_REMOTE,
                actual_channel,
                actual_id,
                actual_url,
                {"reason": "remote_marker_channel_mismatch"},
            )
        return PublicationOutcome(
            FindingPublicationState.PUBLISHED,
            actual_channel,
            actual_id,
            actual_url,
        )
    if remote_id is not None:
        return PublicationOutcome(
            FindingPublicationState.REMOTE_MISSING,
            expected_channel,
            remote_id,
            remote_url,
            {"reason": "remote_content_missing"},
        )
    return None


class GitHubReviewProvider:
    """Read review snapshots and publish findings through GitHub's review API."""

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

    def publish_finding(
        self,
        pull_request_id: str,
        *,
        expected_head_sha: str,
        fingerprint: str,
        concern: ReviewConcern | str,
        summary: str,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        remote_id: str | None = None,
        remote_url: str | None = None,
    ) -> PublicationOutcome:
        try:
            resolved_concern = ReviewConcern(concern)
        except ValueError as exc:
            raise ProviderError("Review finding has an invalid concern") from exc
        if not summary.strip() or len(summary) > 1_000:
            raise ProviderError("Review finding summary is invalid")
        if file_path is None:
            if start_line is not None or end_line is not None:
                raise ProviderError("Review finding anchor is incomplete")
        elif (
            not file_path.strip()
            or file_path.startswith("/")
            or "\\" in file_path
            or any(part in {"", ".", ".."} for part in file_path.split("/"))
            or type(start_line) is not int
            or type(end_line) is not int
            or start_line < 1
            or end_line < start_line
        ):
            raise ProviderError("Review finding anchor is invalid")

        expected_head_sha = expected_head_sha.strip()
        marker = finding_publication_marker(
            pull_request_id, expected_head_sha, fingerprint
        )
        channel = (
            FindingPublicationChannel.REVIEW_THREAD
            if file_path is not None
            else FindingPublicationChannel.REVIEW_BODY
        )
        target = self.get_pull_request(pull_request_id)
        if not target.ready or target.head_sha != expected_head_sha:
            return PublicationOutcome(
                FindingPublicationState.STALE,
                channel,
                remote_id,
                remote_url,
                {"reason": "current_head_mismatch"},
            )

        existing = _publication_from_evidence(
            target.evidence_json, marker, channel, remote_id, remote_url
        )
        if existing is not None:
            return existing

        redacted_summary = self._redact(summary.strip())
        if not isinstance(redacted_summary, str):
            raise ProviderError("Review finding summary could not be redacted")
        review_text = f"[{resolved_concern.value}] {redacted_summary}\n\n{marker}"
        review_input: dict[str, object] = {
            "pullRequestId": _pull_request_node_id(target.evidence_json),
            "commitOID": expected_head_sha,
            "event": "COMMENT",
        }
        if channel is FindingPublicationChannel.REVIEW_BODY:
            review_input["body"] = review_text
        else:
            assert (
                file_path is not None
                and start_line is not None
                and end_line is not None
            )
            thread: dict[str, object] = {
                "body": review_text,
                "path": file_path,
                "line": end_line,
                "side": "RIGHT",
            }
            if start_line != end_line:
                thread["startLine"] = start_line
                thread["startSide"] = "RIGHT"
            review_input["threads"] = [thread]

        data = self._configured_client().execute(
            PUBLISH_PULL_REQUEST_REVIEW_MUTATION, {"input": review_input}
        )
        mutation = _optional_mapping(data.get("addPullRequestReview"))
        review = _optional_mapping(mutation.get("pullRequestReview"))
        created_id = review.get("id")
        created_url = review.get("url")
        if not isinstance(created_id, str) or not created_id:
            raise ProviderError("GitHub review mutation omitted its review id")
        if not isinstance(created_url, str) or not created_url:
            raise ProviderError("GitHub review mutation omitted its review URL")

        confirmed = self.get_pull_request(pull_request_id)
        reconciled = _publication_from_evidence(
            confirmed.evidence_json, marker, channel, created_id, created_url
        )
        if not confirmed.ready or confirmed.head_sha != expected_head_sha:
            return PublicationOutcome(
                FindingPublicationState.STALE,
                channel,
                None if reconciled is None else reconciled.remote_id,
                created_url if reconciled is None else reconciled.remote_url,
                {"reason": "current_head_changed_after_publication"},
            )
        if (
            reconciled is None
            or reconciled.state is not FindingPublicationState.PUBLISHED
        ):
            raise ProviderError("GitHub did not confirm the published finding")
        return reconciled


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
