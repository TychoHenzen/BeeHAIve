"""Provider boundaries for GitHub discovery and pull-request handoff."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .checks import aggregate_check_verdict, normalize_check_rollup
from .models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
    Stage,
    project_stage_from_status,
)

PROVIDER_REQUEST_TIMEOUT = 30.0
DEFAULT_DISCOVERY_CACHE_SECONDS = 600.0
DISCOVERY_CACHE_SECONDS_ENV = "BEEHAIIVE_GITHUB_DISCOVERY_CACHE_SECONDS"


class ProviderError(RuntimeError):
    """Raised when a provider cannot discover or hand off work."""


class GitHubRateLimitError(ProviderError):
    """Raised when GitHub asks the client to stop GraphQL requests temporarily."""

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        retry_after: float | None = None,
        primary: bool = False,
    ) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after
        self.primary = primary


def _header_value(response: object, name: str) -> str | None:
    """Read a response header from urllib responses and test doubles."""

    wanted = name.lower()
    headers: object = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for key, value in cast(Mapping[object, object], headers).items():
            if str(key).lower() == wanted:
                return str(value)
    elif headers is not None:
        items = getattr(cast(Any, headers), "items", None)
        if callable(items):
            header_items = cast(Iterable[tuple[object, object]], items())
            for key, value in header_items:
                if str(key).lower() == wanted:
                    return str(value)

    return None


def _header_float(response: object, name: str) -> float | None:
    value = _header_value(response, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _rate_error_details(errors: object) -> tuple[str, bool] | None:
    if not isinstance(errors, list):
        return None
    for raw_error in cast(list[object], errors):
        if not isinstance(raw_error, Mapping):
            continue
        error = cast(Mapping[str, object], raw_error)
        error_type = str(error.get("type", "")).upper()
        code = str(error.get("code", "")).lower()
        message = str(error.get("message", ""))
        normalized_message = message.lower()
        if not (
            error_type in {"RATE_LIMIT", "RATE_LIMITED"}
            or code in {"graphql_rate_limit", "rate_limit", "rate_limited"}
            or "rate limit" in normalized_message
        ):
            continue
        primary = code == "graphql_rate_limit" or error_type == "RATE_LIMIT"
        return message or "GitHub GraphQL rate limit exceeded", primary
    return None


def _discovery_cache_seconds_from_environment() -> float:
    raw_value = os.environ.get(
        DISCOVERY_CACHE_SECONDS_ENV, str(DEFAULT_DISCOVERY_CACHE_SECONDS)
    )
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ProviderError(
            f"{DISCOVERY_CACHE_SECONDS_ENV} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise ProviderError(
            f"{DISCOVERY_CACHE_SECONDS_ENV} must be a finite non-negative number"
        )
    return value


class GraphQLClient(Protocol):
    """Small client boundary that can be replaced by a GitHub API test double."""

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        """Execute a GraphQL operation and return its data object."""

        ...


class UrllibGraphQLClient:
    """Minimal GitHub GraphQL client using only the Python standard library."""

    def __init__(
        self,
        token: str,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._cooldown_lock = Lock()
        self._cooldown_until = 0.0
        self._cooldown_reset_at: float | None = None
        self._cooldown_retry_after: float | None = None
        self._cooldown_primary = False

    def _raise_if_cooling_down(self) -> None:
        now = time.time()
        with self._cooldown_lock:
            if self._cooldown_until <= now:
                return
            reset_at = self._cooldown_reset_at or self._cooldown_until
            retry_after = self._cooldown_retry_after
            primary = self._cooldown_primary
        raise GitHubRateLimitError(
            "GitHub GraphQL rate limit cooldown is active",
            reset_at=reset_at,
            retry_after=retry_after,
            primary=primary,
        )

    def _set_cooldown(
        self,
        *,
        reset_at: float | None,
        retry_after: float | None,
        primary: bool,
    ) -> float:
        now = time.time()
        if primary and reset_at is not None and reset_at > now:
            cooldown_until = reset_at
        elif retry_after is not None:
            cooldown_until = now + max(retry_after, 0.0)
        elif reset_at is not None and reset_at > now:
            cooldown_until = reset_at
        else:
            cooldown_until = now + 60.0

        with self._cooldown_lock:
            if cooldown_until > self._cooldown_until:
                self._cooldown_until = cooldown_until
                self._cooldown_reset_at = reset_at
                self._cooldown_retry_after = retry_after
                self._cooldown_primary = primary
            return self._cooldown_until

    def _rate_limit_error(
        self,
        response: object,
        errors: object = None,
        *,
        status_code: int | None = None,
    ) -> GitHubRateLimitError | None:
        details = _rate_error_details(errors)
        remaining = _header_float(response, "x-ratelimit-remaining")
        reset_at = _header_float(response, "x-ratelimit-reset")
        retry_after = _header_float(response, "retry-after")
        status_is_rate_limited = status_code == 429 or (
            status_code == 403
            and (retry_after is not None or (remaining is not None and remaining <= 0))
        )
        has_rate_limit_header = (
            remaining is not None and remaining <= 0 and errors is not None
        )
        if details is None and not has_rate_limit_header and not status_is_rate_limited:
            return None

        default_primary = status_code not in {403, 429}
        message, primary = details or (
            "GitHub GraphQL rate limit exceeded",
            default_primary,
        )
        cooldown_until = self._set_cooldown(
            reset_at=reset_at,
            retry_after=retry_after,
            primary=primary,
        )
        effective_reset_at = reset_at or cooldown_until
        kind = "primary" if primary else "secondary"
        return GitHubRateLimitError(
            f"GitHub GraphQL {kind} rate limit exceeded: {message}",
            reset_at=effective_reset_at,
            retry_after=retry_after,
            primary=primary,
        )

    def _record_exhausted_headers(self, response: object) -> None:
        remaining = _header_float(response, "x-ratelimit-remaining")
        if remaining is not None and remaining <= 0:
            self._set_cooldown(
                reset_at=_header_float(response, "x-ratelimit-reset"),
                retry_after=_header_float(response, "retry-after"),
                primary=True,
            )

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        self._raise_if_cooling_down()
        request = Request(
            self._endpoint,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(
                "utf-8"
            ),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response_headers: object | None = None
        try:
            with urlopen(request, timeout=PROVIDER_REQUEST_TIMEOUT) as response:
                response_headers = response
                raw_payload: object = json.loads(response.read())
        except HTTPError as exc:
            rate_error = self._rate_limit_error(exc, status_code=exc.code)
            if rate_error is not None:
                raise rate_error from exc
            raise ProviderError(f"GitHub GraphQL request failed: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"GitHub GraphQL returned invalid JSON: {exc}") from exc
        except OSError as exc:
            raise ProviderError(f"GitHub GraphQL request failed: {exc}") from exc

        if not isinstance(raw_payload, dict):
            raise ProviderError("GitHub GraphQL returned a non-object response")
        payload = cast(dict[str, Any], raw_payload)
        errors = payload.get("errors")
        rate_error = self._rate_limit_error(response_headers, errors)
        if rate_error is not None:
            raise rate_error
        self._record_exhausted_headers(response_headers)
        if errors:
            raise ProviderError(f"GitHub GraphQL returned errors: {errors}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError("GitHub GraphQL response did not contain data")
        return cast(dict[str, Any], data)


class ProjectProvider(Protocol):
    """Provider used by the orchestrator for discovery and handoff."""

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        """Return the selected project and every linked repository."""

        ...

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        """Create or reuse the branch and pull request for a PBI run."""

        ...

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        """Resolve and return the base branch before a handoff is persisted."""

        ...

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        """Validate handoff names and return the existing base branch."""

        ...

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        """Return current pull-request identity and mergeability evidence."""

        ...

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        """Update only the existing source branch with an expected-head guard."""

        ...


PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      title
    }
  }
}
"""

REPOSITORIES_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      repositories(first: 100, after: $cursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        nodes {
          content {
            __typename
            ... on Issue {
              number
              title
              url
              repository { nameWithOwner }
              # Keep the project-wide query below GitHub's node limit. The
              # provider completes these connections with repository queries.
              labels(first: 20) {
                nodes { name }
                pageInfo { hasNextPage endCursor }
              }
              subIssues(first: 20) {
                nodes {
                  number
                  title
                  state
                  labels(first: 20) {
                    nodes { name }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
              comments(first: 20) {
                nodes {
                  author { ... on User { login } ... on Bot { login } }
                  body
                  createdAt
                  url
                }
                pageInfo { hasNextPage endCursor }
              }
              closedByPullRequestsReferences(includeClosedPrs: true, first: 20) {
                nodes {
                  number
                  url
                  state
                  merged
                  headRefName
                  headRef { name }
                  reviewDecision
                  reviewRequests(first: 20) {
                    nodes {
                      requestedReviewer {
                        ... on User { login }
                        ... on Team { name }
                      }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                  latestReviews(first: 20) {
                    nodes {
                      author { ... on User { login } ... on Bot { login } }
                      state
                      body
                      submittedAt
                      url
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_LABELS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      labels(first: 100, after: $cursor) {
        nodes { name }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $cursor) {
        nodes {
          number
          title
          state
          labels(first: 100) {
            nodes { name }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 50, after: $cursor) {
        nodes {
          author { ... on User { login } ... on Bot { login } }
          body
          createdAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_PULL_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(
        includeClosedPrs: true
        first: 100
        after: $cursor
      ) {
        nodes {
          number
          url
          state
          merged
          headRefName
          headRef { name target { oid } }
          reviewDecision
          reviewRequests(first: 100) {
            nodes {
              requestedReviewer {
                ... on User { login }
                ... on Team { name }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
          latestReviews(first: 100) {
            nodes {
              author { ... on User { login } ... on Bot { login } }
              state
              body
              submittedAt
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

PULL_REQUEST_CHECKS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      headRef { target { oid } }
      statusCheckRollup {
        commit { oid }
        state
        contexts(first: 100, after: $cursor) {
          nodes {
            __typename
            ... on CheckRun {
              name
              status
              conclusion
              isRequired(pullRequestNumber: $number)
              detailsUrl
              startedAt
              completedAt
            }
            ... on StatusContext {
              context
              state
              isRequired(pullRequestNumber: $number)
              targetUrl
              createdAt
              updatedAt
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""

PULL_REQUEST_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      url
      state
      merged
      headRefName
      headRef { name target { oid } }
      baseRefName
      baseRef { name target { oid } }
      mergeable
      mergeStateStatus
    }
  }
}
"""

PULL_REQUEST_REVIEW_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100, after: $cursor) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Team { name }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_LATEST_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      latestReviews(first: 100, after: $cursor) {
        nodes {
          author { ... on User { login } ... on Bot { login } }
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

REPOSITORY_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedBranch: String!
  $pullRequestCursor: String
) {
  repository(owner: $owner, name: $name) {
    id
    defaultBranchRef { name target { oid } }
    ref(qualifiedName: $qualifiedBranch) { name }
    pullRequests(
      first: 100
      after: $pullRequestCursor
      states: [OPEN, CLOSED, MERGED]
    ) {
      nodes { number url headRefName baseRefName body }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

DEFAULT_BRANCH_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
  }
}
"""

BASE_BRANCH_QUERY = """
query($owner: String!, $name: String!, $qualifiedBranch: String!) {
  repository(owner: $owner, name: $name) {
    baseRef: ref(qualifiedName: $qualifiedBranch) { name target { oid } }
  }
}
"""

CREATE_REF_MUTATION = """
mutation($input: CreateRefInput!) {
  createRef(input: $input) { ref { name } }
}
"""

CREATE_PULL_REQUEST_MUTATION = """
mutation($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest { number url }
  }
}
"""


def _required_text(value: object, label: str) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _pull_request_snapshot(
    repository: str, requested_number: int, value: object
) -> PullRequestSnapshot:
    pull_request = _mapping(value)
    number = pull_request.get("number")
    if not isinstance(number, int) or number != requested_number:
        raise ProviderError("GitHub returned the wrong pull-request number")
    pull_request_id = _required_text(pull_request.get("id"), "pull-request id")
    url = _required_text(pull_request.get("url"), "pull-request URL")
    state = _required_text(pull_request.get("state"), "pull-request state")
    merged = pull_request.get("merged")
    if (
        pull_request_id is None
        or url is None
        or state is None
        or not isinstance(merged, bool)
    ):
        raise ProviderError("GitHub returned incomplete pull-request identity")

    head_ref = pull_request.get("headRef")
    base_ref = pull_request.get("baseRef")
    source_ref = (
        cast(Mapping[str, Any], head_ref)
        if isinstance(head_ref, Mapping)
        else cast(Mapping[str, Any], {})
    )
    target_ref = (
        cast(Mapping[str, Any], base_ref)
        if isinstance(base_ref, Mapping)
        else cast(Mapping[str, Any], {})
    )
    source_branch = _required_text(pull_request.get("headRefName"), "source branch")
    source_ref_name = _required_text(source_ref.get("name"), "source branch")
    source_head = _pull_request_head_sha(cast(object, head_ref))
    target_branch = _required_text(pull_request.get("baseRefName"), "target branch")
    target_ref_name = _required_text(target_ref.get("name"), "target branch")
    target_head = _pull_request_head_sha(cast(object, base_ref))
    if (
        source_branch is None
        or source_ref_name != source_branch
        or source_head is None
        or target_branch is None
        or target_ref_name != target_branch
        or target_head is None
    ):
        return PullRequestSnapshot(
            repository,
            number,
            pull_request_id,
            url,
            state.upper(),
            merged,
            source_branch,
            source_head,
            target_branch,
            target_head,
            _required_text(pull_request.get("mergeable"), "mergeability"),
            _required_text(pull_request.get("mergeStateStatus"), "merge state"),
            "GitHub returned contradictory pull-request branch identity",
        )
    mergeable = _required_text(pull_request.get("mergeable"), "mergeability")
    merge_state = _required_text(pull_request.get("mergeStateStatus"), "merge state")
    return PullRequestSnapshot(
        repository,
        number,
        pull_request_id,
        url,
        state.upper(),
        merged,
        source_branch,
        source_head,
        target_branch,
        target_head,
        mergeable.upper() if mergeable else None,
        merge_state.upper() if merge_state else None,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError("GitHub GraphQL returned an invalid object")
    return cast(Mapping[str, Any], value)


def _nodes(value: object) -> list[Mapping[str, Any]]:
    container = _mapping(value)
    raw_nodes: object = container.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ProviderError("GitHub GraphQL returned invalid nodes")
    node_values = cast(list[object], raw_nodes)
    return [_mapping(node) for node in node_values if node is not None]


def _project(data: Mapping[str, Any], owner_type: str = "user") -> Mapping[str, Any]:
    owner = _mapping(data.get(owner_type))
    return _mapping(owner.get("projectV2"))


def _owner_query(query: str, owner_type: str) -> str:
    if owner_type not in {"user", "organization"}:
        raise ProviderError("GitHub Project owner type must be user or organization")
    return query.replace("user(login:", f"{owner_type}(login:")


def _next_cursor(connection: Mapping[str, Any]) -> tuple[bool, str | None]:
    page_info = _mapping(connection.get("pageInfo", {}))
    has_next = page_info.get("hasNextPage") is True
    cursor = page_info.get("endCursor")
    if has_next and not isinstance(cursor, str):
        raise ProviderError("GitHub GraphQL page did not include an end cursor")
    return has_next, cursor if isinstance(cursor, str) else None


def _connection_at(data: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any]:
    value: object = data
    for key in path:
        value = _mapping(value).get(key)
    return _mapping(value)


def _complete_connection(
    client: GraphQLClient,
    initial: object,
    query: str,
    variables: Mapping[str, object],
    response_path: tuple[str, ...],
) -> dict[str, Any]:
    connection = _mapping(initial)
    nodes = list(_nodes(connection))
    has_next, cursor = _next_cursor(connection)
    while has_next:
        page_data = client.execute(
            query,
            {**variables, "cursor": cursor},
        )
        page = _connection_at(page_data, response_path)
        nodes.extend(_nodes(page))
        has_next, cursor = _next_cursor(page)
    completed = dict(connection)
    completed["nodes"] = nodes
    completed["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    return completed


def _stage_from_status(status: str | None) -> Stage | None:
    return project_stage_from_status(status)


def _actor_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    actor = cast(Mapping[str, Any], value)
    for key in ("login", "name"):
        name = actor.get(key)
        if isinstance(name, str) and name:
            return name
    return None


def _label_names(value: object) -> list[str]:
    names: list[str] = []
    for label in _nodes(value):
        name = label.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _pull_request_head_sha(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    head_ref = cast(Mapping[str, Any], value)
    target_value = head_ref.get("target")
    if not isinstance(target_value, Mapping):
        return None
    target = cast(Mapping[str, Any], target_value)
    oid = target.get("oid")
    return oid if isinstance(oid, str) and oid.strip() else None


def _commit_oid(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _review_status(state: object) -> str:
    normalized = str(state or "").strip().upper()
    if normalized == "APPROVED":
        return "pass"
    if normalized == "CHANGES_REQUESTED":
        return "fail"
    return "pending"


def _dashboard_metadata(issue: Mapping[str, Any]) -> dict[str, object]:
    """Project issue metadata that the dashboard can show without fake state."""

    metadata: dict[str, object] = {}
    source_url = issue.get("url")
    if isinstance(source_url, str):
        metadata["source_url"] = source_url
    labels = _label_names(issue.get("labels", {}))

    subtasks: list[dict[str, object]] = []
    for raw_subtask in _nodes(issue.get("subIssues", {})):
        number = raw_subtask.get("number")
        title = raw_subtask.get("title")
        if not isinstance(number, int) or not isinstance(title, str):
            continue
        subtask: dict[str, object] = {
            "id": f"#{number}",
            "number": number,
            "title": title,
        }
        state = raw_subtask.get("state")
        if isinstance(state, str):
            subtask["status"] = state.lower()
        subtask_labels = _label_names(raw_subtask.get("labels", {}))
        if subtask_labels:
            subtask["labels"] = subtask_labels
        subtasks.append(subtask)
    if subtasks:
        metadata["subtasks"] = subtasks

    readers: list[dict[str, object]] = []
    reviewers: dict[str, dict[str, object]] = {}
    pull_requests: list[dict[str, object]] = []
    active_check_snapshots: list[Mapping[str, object]] = []
    for raw_pull_request in _nodes(issue.get("closedByPullRequestsReferences", {})):
        pull_request_number = raw_pull_request.get("number")
        if not isinstance(pull_request_number, int):
            continue
        pull_request_readers: list[dict[str, object]] = []
        pull_request_reviewers: dict[str, dict[str, object]] = {}
        for raw_request in _nodes(raw_pull_request.get("reviewRequests", {})):
            name = _actor_name(raw_request.get("requestedReviewer"))
            if name is None:
                continue
            reader: dict[str, object] = {
                "id": f"#{pull_request_number}:{name}",
                "name": name,
                "pull_request": pull_request_number,
                "status": "pending",
            }
            pull_request_readers.append(reader)
            readers.append(reader)

        for raw_review in _nodes(raw_pull_request.get("latestReviews", {})):
            name = _actor_name(raw_review.get("author"))
            if name is None:
                continue
            status = _review_status(raw_review.get("state"))
            reviewer: dict[str, object] = {
                "status": status,
                "pull_request": pull_request_number,
            }
            body = raw_review.get("body")
            if isinstance(body, str) and body.strip():
                reviewer["comment"] = body
            submitted_at = raw_review.get("submittedAt")
            if isinstance(submitted_at, str):
                reviewer["submitted_at"] = submitted_at
            reviewer_key = f"#{pull_request_number}:{name}"
            pull_request_reviewers[reviewer_key] = reviewer
            reviewers[reviewer_key] = reviewer
            for reader in pull_request_readers:
                if reader["name"] == name:
                    reader["status"] = status
                    break
            else:
                reader = {
                    "id": reviewer_key,
                    "name": name,
                    "pull_request": pull_request_number,
                    "status": status,
                }
                pull_request_readers.append(reader)
                readers.append(reader)

        pull_request: dict[str, object] = {
            "number": pull_request_number,
            "readers": pull_request_readers,
            "reviewers": pull_request_reviewers,
        }
        url = raw_pull_request.get("url")
        if isinstance(url, str):
            pull_request["url"] = url
        state = raw_pull_request.get("state")
        if isinstance(state, str) and state.strip():
            pull_request["state"] = state.lower()
        merged = raw_pull_request.get("merged")
        if isinstance(merged, bool):
            pull_request["merged"] = merged
        source_branch = raw_pull_request.get("headRefName")
        has_source_branch = isinstance(source_branch, str) and bool(
            source_branch.strip()
        )
        if has_source_branch:
            pull_request["source_branch"] = source_branch
        head_ref = raw_pull_request.get("headRef")
        head_sha = _pull_request_head_sha(head_ref)
        if head_sha is not None:
            pull_request["head_sha"] = head_sha
        pull_request["source_branch_state"] = (
            "unknown"
            if "headRef" not in raw_pull_request or not has_source_branch
            else "deleted"
            if head_ref is None
            else "present"
            if isinstance(head_ref, Mapping)
            else "unknown"
        )
        decision = raw_pull_request.get("reviewDecision")
        if isinstance(decision, str):
            pull_request["review_decision"] = decision.lower()
        checks = raw_pull_request.get("checks")
        if isinstance(checks, Mapping):
            normalized_checks = dict(cast(Mapping[str, object], checks))
            normalized_checks["number"] = pull_request_number
            pull_request["checks"] = normalized_checks
            if str(pull_request.get("state", "")).lower() == "open":
                active_check_snapshots.append(normalized_checks)
        pull_requests.append(pull_request)

    if readers:
        metadata["readers"] = readers
    if reviewers:
        metadata["reviewers"] = reviewers
    if pull_requests:
        metadata["pull_requests"] = pull_requests
    metadata["checks"] = {
        "verdict": aggregate_check_verdict(active_check_snapshots),
        "pull_requests": [dict(check) for check in active_check_snapshots],
    }

    activity: list[dict[str, object]] = []
    for comment in _nodes(issue.get("comments", {})):
        body = comment.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        entry: dict[str, object] = {
            "agent": _actor_name(comment.get("author")) or "github",
            "action": body,
        }
        for source_key, target_key in (("createdAt", "time"), ("url", "url")):
            value = comment.get(source_key)
            if isinstance(value, str):
                entry[target_key] = value
        activity.append(entry)
    if activity:
        metadata["activity"] = activity

    bounce_count = 0
    for label in labels:
        match = re.fullmatch(r"bounces?/(\d+)", label.strip(), re.IGNORECASE)
        if match:
            bounce_count = max(bounce_count, int(match.group(1)))
    escalation_log = [
        {"tier": label.split("/", 1)[1], "resolved": False}
        for label in labels
        if label.lower().startswith("escalation/") and "/" in label
    ]
    if bounce_count or escalation_log:
        escalation: dict[str, object] = {
            "current": bounce_count,
            "consecutive": bounce_count,
        }
        if escalation_log:
            escalation["current_tier"] = escalation_log[-1]["tier"]
        metadata["escalation"] = escalation
        metadata["escalation_log"] = escalation_log

    return metadata


def _validate_branch_name(branch: str) -> None:
    if (
        not branch
        or branch.strip() != branch
        or branch in {".", "..", "@"}
        or ".." in branch
        or "@{" in branch
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.startswith(".")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or "//" in branch
        or any(ord(char) < 32 or char in " ~^:?*[\\" for char in branch)
    ):
        raise ProviderError("GitHub branch name is invalid")


def _handoff_marker(request: HandoffRequest) -> str:
    if not request.run_id.strip():
        raise ProviderError("Handoff run identity is required")
    payload = json.dumps(
        {
            "pbi_number": request.pbi_number,
            "project_id": request.project_id,
            "repository": request.repository,
            "run_id": request.run_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"<!-- beehaiive-handoff:{encoded} -->"


def _handoff_body(body: str, marker: str) -> str:
    if marker in body:
        return body
    return f"{body.rstrip()}\n\n{marker}" if body.strip() else marker


def _pull_request_matches(
    pull_request: Mapping[str, Any],
    branch: str,
    base_branch: str,
    identity_marker: str,
) -> bool:
    return (
        pull_request.get("headRefName") == branch
        and pull_request.get("baseRefName") == base_branch
        and isinstance(pull_request.get("body"), str)
        and identity_marker in pull_request["body"]
    )


class GitHubProjectProvider:
    """GitHub ProjectV2 provider for a user- or organization-owned project."""

    def __init__(
        self,
        owner: str,
        project_number: int,
        token: str,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
        owner_type: str = "user",
        discovery_cache_seconds: float = DEFAULT_DISCOVERY_CACHE_SECONDS,
    ) -> None:
        if owner_type not in {"user", "organization"}:
            raise ProviderError(
                "GitHub Project owner type must be user or organization"
            )
        if not math.isfinite(discovery_cache_seconds) or discovery_cache_seconds < 0:
            raise ProviderError(
                "GitHub discovery cache seconds must be a finite non-negative number"
            )
        self.owner = owner
        self.project_number = project_number
        self.owner_type = owner_type
        self.project_id = f"{owner}:{project_number}"
        self._client = client or UrllibGraphQLClient(token, endpoint)
        self._discovery_cache_seconds = discovery_cache_seconds
        self._discovery_cache: tuple[float, ProjectSnapshot] | None = None
        self._discovery_lock = Lock()

    def _complete_issue_metadata(self, issue: Mapping[str, Any]) -> Mapping[str, Any]:
        repository_name = _mapping(issue.get("repository")).get("nameWithOwner")
        issue_number = issue.get("number")
        if not isinstance(repository_name, str) or not isinstance(issue_number, int):
            return issue
        repository_owner, repository = self._repository_parts(repository_name)
        variables = {
            "owner": repository_owner,
            "name": repository,
            "number": issue_number,
        }
        completed_issue = dict(issue)
        completed_issue["labels"] = _complete_connection(
            self._client,
            issue.get("labels", {}),
            ISSUE_LABELS_QUERY,
            variables,
            ("repository", "issue", "labels"),
        )
        subissues = _complete_connection(
            self._client,
            issue.get("subIssues", {}),
            ISSUE_SUB_ISSUES_QUERY,
            variables,
            ("repository", "issue", "subIssues"),
        )
        completed_subissues: list[dict[str, Any]] = []
        for raw_subissue in _nodes(subissues):
            subissue = dict(raw_subissue)
            subissue_number = subissue.get("number")
            if isinstance(subissue_number, int):
                subissue["labels"] = _complete_connection(
                    self._client,
                    subissue.get("labels", {}),
                    ISSUE_LABELS_QUERY,
                    {
                        "owner": repository_owner,
                        "name": repository,
                        "number": subissue_number,
                    },
                    ("repository", "issue", "labels"),
                )
            completed_subissues.append(subissue)
        subissues["nodes"] = completed_subissues
        completed_issue["subIssues"] = subissues
        completed_issue["comments"] = _complete_connection(
            self._client,
            issue.get("comments", {}),
            ISSUE_COMMENTS_QUERY,
            variables,
            ("repository", "issue", "comments"),
        )
        pull_requests = _complete_connection(
            self._client,
            issue.get("closedByPullRequestsReferences", {}),
            ISSUE_PULL_REQUESTS_QUERY,
            variables,
            ("repository", "issue", "closedByPullRequestsReferences"),
        )
        completed_pull_requests: list[dict[str, Any]] = []
        for raw_pull_request in _nodes(pull_requests):
            pull_request = dict(raw_pull_request)
            pull_request_number = pull_request.get("number")
            if isinstance(pull_request_number, int):
                if str(pull_request.get("state", "")).upper() == "OPEN":
                    checks = self._complete_pull_request_checks(
                        repository_owner,
                        repository,
                        pull_request_number,
                        _pull_request_head_sha(pull_request.get("headRef")),
                    )
                    pull_request["checks"] = checks
                    observed_head_sha = checks.get("head_sha")
                    if isinstance(observed_head_sha, str) and observed_head_sha:
                        pull_request["headRef"] = {"target": {"oid": observed_head_sha}}
                review_variables = {
                    "owner": repository_owner,
                    "name": repository,
                    "number": pull_request_number,
                }
                pull_request["reviewRequests"] = _complete_connection(
                    self._client,
                    pull_request.get("reviewRequests", {}),
                    PULL_REQUEST_REVIEW_REQUESTS_QUERY,
                    review_variables,
                    ("repository", "pullRequest", "reviewRequests"),
                )
                pull_request["latestReviews"] = _complete_connection(
                    self._client,
                    pull_request.get("latestReviews", {}),
                    PULL_REQUEST_LATEST_REVIEWS_QUERY,
                    review_variables,
                    ("repository", "pullRequest", "latestReviews"),
                )
            completed_pull_requests.append(pull_request)
        pull_requests["nodes"] = completed_pull_requests
        completed_issue["closedByPullRequestsReferences"] = pull_requests
        return completed_issue

    def _complete_pull_request_checks(
        self,
        owner: str,
        repository: str,
        number: int,
        head_sha: str | None,
    ) -> dict[str, object]:
        if head_sha is None:
            return normalize_check_rollup(None, None)
        variables = {"owner": owner, "name": repository, "number": number}
        try:
            data = self._client.execute(PULL_REQUEST_CHECKS_QUERY, variables)
            pull_request = _mapping(_mapping(data.get("repository")).get("pullRequest"))
            observed_head_sha = _pull_request_head_sha(pull_request.get("headRef"))
            if str(pull_request.get("state", "")).upper() != "OPEN":
                return normalize_check_rollup(observed_head_sha, None)
            rollup = _mapping(pull_request.get("statusCheckRollup"))
            initial_contexts = _mapping(rollup.get("contexts"))
            context_nodes = list(_nodes(initial_contexts))
            has_next, cursor = _next_cursor(initial_contexts)
            rollup_sha = _commit_oid(_mapping(rollup.get("commit")).get("oid"))
            while has_next:
                page_data = self._client.execute(
                    PULL_REQUEST_CHECKS_QUERY,
                    {**variables, "cursor": cursor},
                )
                page_pull_request = _mapping(
                    _mapping(page_data.get("repository")).get("pullRequest")
                )
                page_head_sha = _pull_request_head_sha(page_pull_request.get("headRef"))
                page_rollup = _mapping(page_pull_request.get("statusCheckRollup"))
                page_rollup_sha = _commit_oid(
                    _mapping(page_rollup.get("commit")).get("oid")
                )
                if (
                    str(page_pull_request.get("state", "")).upper() != "OPEN"
                    or page_head_sha != observed_head_sha
                    or page_rollup_sha != rollup_sha
                ):
                    return normalize_check_rollup(page_head_sha, None)
                page_contexts = _mapping(page_rollup.get("contexts"))
                context_nodes.extend(_nodes(page_contexts))
                has_next, cursor = _next_cursor(page_contexts)
            completed_rollup = dict(rollup)
            completed_rollup["contexts"] = {
                "nodes": context_nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            return normalize_check_rollup(observed_head_sha, completed_rollup)
        except ProviderError as exc:
            return normalize_check_rollup(None, None, error=str(exc))

    @classmethod
    def from_environment(cls) -> GitHubProjectProvider:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        owner = os.environ.get("GITHUB_PROJECT_OWNER")
        number_text = os.environ.get("GITHUB_PROJECT_NUMBER")
        owner_type = os.environ.get("GITHUB_PROJECT_OWNER_TYPE", "user")
        if not token or not owner or not number_text:
            raise ProviderError(
                "Set GITHUB_TOKEN, GITHUB_PROJECT_OWNER, and GITHUB_PROJECT_NUMBER"
            )
        try:
            number = int(number_text)
        except ValueError as exc:
            raise ProviderError("GITHUB_PROJECT_NUMBER must be an integer") from exc
        return cls(
            owner,
            number,
            token,
            owner_type=owner_type,
            discovery_cache_seconds=_discovery_cache_seconds_from_environment(),
        )

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        if project_id != self.project_id:
            raise ProviderError(
                f"Provider is configured for {self.project_id}, not {project_id}"
            )

        with self._discovery_lock:
            cached = self._discovery_cache
            if (
                cached is not None
                and time.monotonic() - cached[0] < self._discovery_cache_seconds
            ):
                return cached[1]
            try:
                snapshot = self._discover_project_uncached()
            except GitHubRateLimitError:
                if cached is None:
                    raise
                return cached[1]
            self._discovery_cache = (time.monotonic(), snapshot)
            return snapshot

    def invalidate_discovery_cache(self) -> None:
        with self._discovery_lock:
            self._discovery_cache = None

    def _discover_project_uncached(self) -> ProjectSnapshot:

        data = self._client.execute(
            _owner_query(PROJECT_QUERY, self.owner_type),
            {
                "owner": self.owner,
                "number": self.project_number,
            },
        )
        project = _project(data, self.owner_type)
        repositories: dict[str, list[PbiSnapshot]] = {}
        repository_cursor: str | None = None
        while True:
            repository_data = self._client.execute(
                _owner_query(REPOSITORIES_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": repository_cursor,
                },
            )
            repository_connection = _mapping(
                _project(repository_data, self.owner_type).get("repositories")
            )
            for repository in _nodes(repository_connection):
                name = repository.get("nameWithOwner")
                if isinstance(name, str) and name:
                    repositories.setdefault(name, [])
            has_next, repository_cursor = _next_cursor(repository_connection)
            if not has_next:
                break

        item_cursor: str | None = None
        while True:
            item_data = self._client.execute(
                _owner_query(ITEMS_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": item_cursor,
                },
            )
            item_connection = _mapping(
                _project(item_data, self.owner_type).get("items")
            )
            for item in _nodes(item_connection):
                content_value = item.get("content")
                if content_value is None:
                    continue
                content = _mapping(content_value)
                if content.get("__typename") != "Issue":
                    continue
                repository = _mapping(content.get("repository"))
                repository_name = repository.get("nameWithOwner")
                number = content.get("number")
                title = content.get("title")
                if (
                    not isinstance(repository_name, str)
                    or not isinstance(number, int)
                    or not isinstance(title, str)
                ):
                    continue
                content = self._complete_issue_metadata(content)
                status = None
                for field_value in _nodes(item.get("fieldValues", {})):
                    raw_field = field_value.get("field")
                    if raw_field is None:
                        continue
                    field = _mapping(raw_field)
                    if field.get("name") == "Status" and isinstance(
                        field_value.get("name"), str
                    ):
                        status = field_value["name"]
                        break
                stage = _stage_from_status(status)
                repositories.setdefault(repository_name, []).append(
                    PbiSnapshot(
                        repository_name,
                        number,
                        title,
                        stage,
                        status,
                        stage is not None,
                        _dashboard_metadata(content),
                    )
                )
            has_next, item_cursor = _next_cursor(item_connection)
            if not has_next:
                break

        repository_snapshots = tuple(
            RepositorySnapshot(
                name=name,
                pbis=tuple(sorted(pbis, key=lambda pbi: pbi.number)),
            )
            for name, pbis in sorted(repositories.items())
        )
        project_name = project.get("title")
        if not isinstance(project_name, str):
            raise ProviderError("GitHub Project did not include a title")
        return ProjectSnapshot(self.project_id, project_name, repository_snapshots)

    @staticmethod
    def _repository_parts(repository: str) -> tuple[str, str]:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise ProviderError(f"Repository must use owner/name format: {repository}")
        return owner, name

    def _resolve_custom_base_branch(
        self, owner: str, name: str, requested: str
    ) -> tuple[str, str]:
        data = self._client.execute(
            BASE_BRANCH_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedBranch": f"refs/heads/{requested}",
            },
        )
        base_ref = _mapping(_mapping(data.get("repository")).get("baseRef"))
        resolved_name = base_ref.get("name")
        resolved_oid = _mapping(base_ref.get("target")).get("oid")
        if resolved_name != requested or not isinstance(resolved_oid, str):
            raise ProviderError(
                f"GitHub repository does not contain base branch: {requested}"
            )
        return requested, resolved_oid

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        owner, name = self._repository_parts(repository)
        if requested:
            resolved_name, _ = self._resolve_custom_base_branch(owner, name, requested)
            return resolved_name
        data = self._client.execute(
            DEFAULT_BRANCH_QUERY,
            {"owner": owner, "name": name},
        )
        repository_data = _mapping(data.get("repository"))
        default_branch = _mapping(repository_data.get("defaultBranchRef"))
        name_value = default_branch.get("name")
        if not isinstance(name_value, str) or not name_value:
            raise ProviderError("GitHub repository did not include a default branch")
        return name_value

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        _validate_branch_name(branch)
        return self.resolve_base_branch(repository, requested_base)

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        if number <= 0:
            raise ProviderError("Pull-request number must be positive")
        owner, name = self._repository_parts(repository)
        data = self._client.execute(
            PULL_REQUEST_STATE_QUERY,
            {"owner": owner, "name": name, "number": number},
        )
        repository_data = _mapping(data.get("repository"))
        pull_request = repository_data.get("pullRequest")
        if pull_request is None:
            raise ProviderError(f"Pull request not found: {repository}#{number}")
        return _pull_request_snapshot(repository, number, pull_request)

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        if snapshot.source_head != expected_head:
            raise ProviderError("Pull-request source head changed before update")
        if (
            snapshot.state != "OPEN"
            or snapshot.merged
            or not snapshot.source_branch
            or not expected_head.strip()
            or not repaired_head.strip()
        ):
            raise ProviderError("Pull request is not eligible for source-branch update")
        _validate_branch_name(snapshot.source_branch)
        workspace = Path(worktree).resolve()
        try:
            head_result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
            if (
                head_result.returncode != 0
                or head_result.stdout.strip() != repaired_head
            ):
                raise ProviderError("Repair worktree head does not match repaired head")
            ancestry = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    repaired_head,
                ],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
            if ancestry.returncode != 0:
                raise ProviderError("Repair commit does not preserve source history")
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "push",
                    "--porcelain",
                    f"--force-with-lease=refs/heads/{snapshot.source_branch}:{expected_head}",
                    "origin",
                    f"{repaired_head}:refs/heads/{snapshot.source_branch}",
                ],
                capture_output=True,
                text=True,
                timeout=PROVIDER_REQUEST_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("Guarded source-branch update failed") from exc
        if result.returncode != 0:
            raise ProviderError(
                "Guarded source-branch update rejected with exit code "
                f"{result.returncode}"
            )

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        owner, name = self._repository_parts(request.repository)
        _validate_branch_name(request.branch)
        identity_marker = _handoff_marker(request)
        qualified_branch = f"refs/heads/{request.branch}"
        data = self._client.execute(
            REPOSITORY_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedBranch": qualified_branch,
                "pullRequestCursor": None,
            },
        )
        repository = _mapping(data.get("repository"))
        raw_repository_id = repository.get("id")
        default_branch = _mapping(repository.get("defaultBranchRef"))
        default_branch_name = default_branch.get("name")
        default_oid = _mapping(default_branch.get("target")).get("oid")
        if (
            not isinstance(raw_repository_id, str)
            or not isinstance(default_branch_name, str)
            or not isinstance(default_oid, str)
        ):
            raise ProviderError(
                "GitHub repository did not include branch creation metadata"
            )
        repository_id = raw_repository_id
        base_branch = request.base_branch or default_branch_name
        base_oid = default_oid
        if base_branch != default_branch_name:
            _, base_oid = self._resolve_custom_base_branch(owner, name, base_branch)

        if repository.get("ref") is None:
            try:
                create_data = self._client.execute(
                    CREATE_REF_MUTATION,
                    {
                        "input": {
                            "repositoryId": repository_id,
                            "name": qualified_branch,
                            "oid": base_oid,
                        }
                    },
                )
            except ProviderError as create_error:
                try:
                    retry_data = self._client.execute(
                        REPOSITORY_QUERY,
                        {
                            "owner": owner,
                            "name": name,
                            "qualifiedBranch": qualified_branch,
                            "pullRequestCursor": None,
                        },
                    )
                    retry_repository = _mapping(_mapping(retry_data.get("repository")))
                except ProviderError:
                    raise create_error from None
                if retry_repository.get("ref") is None:
                    raise create_error from None
                repository = retry_repository
            else:
                created_ref = _mapping(
                    _mapping(create_data.get("createRef")).get("ref")
                )
                if created_ref.get("name") != qualified_branch:
                    raise ProviderError(
                        f"GitHub did not confirm branch creation: {qualified_branch}"
                    )

        existing = self._find_existing_pull_request(
            owner,
            name,
            qualified_branch,
            request.branch,
            base_branch,
            identity_marker,
            initial_repository=repository,
        )
        if existing is not None:
            return existing

        try:
            pull_request_data = self._client.execute(
                CREATE_PULL_REQUEST_MUTATION,
                {
                    "input": {
                        "repositoryId": repository_id,
                        "baseRefName": base_branch,
                        "headRefName": request.branch,
                        "title": request.title,
                        "body": _handoff_body(request.body, identity_marker),
                    }
                },
            )
        except ProviderError as create_error:
            try:
                existing = self._find_existing_pull_request(
                    owner,
                    name,
                    qualified_branch,
                    request.branch,
                    base_branch,
                    identity_marker,
                )
            except ProviderError:
                raise create_error from None
            if existing is None:
                raise create_error from None
            return existing
        pull_request = _mapping(
            _mapping(pull_request_data.get("createPullRequest")).get("pullRequest")
        )
        url = pull_request.get("url")
        number = pull_request.get("number")
        if not isinstance(url, str) or not isinstance(number, int):
            raise ProviderError("GitHub did not return a pull-request record")
        return HandoffResult(request.branch, url, number)

    def _find_existing_pull_request(
        self,
        owner: str,
        name: str,
        qualified_branch: str,
        branch: str,
        base_branch: str,
        identity_marker: str,
        initial_repository: Mapping[str, Any] | None = None,
    ) -> HandoffResult | None:
        cursor: str | None = None
        repository = initial_repository
        while True:
            if repository is None:
                data = self._client.execute(
                    REPOSITORY_QUERY,
                    {
                        "owner": owner,
                        "name": name,
                        "qualifiedBranch": qualified_branch,
                        "pullRequestCursor": cursor,
                    },
                )
                repository = _mapping(_mapping(data.get("repository")))
            for pull_request in _nodes(repository.get("pullRequests", {})):
                if _pull_request_matches(
                    pull_request, branch, base_branch, identity_marker
                ):
                    url = pull_request.get("url")
                    number = pull_request.get("number")
                    if isinstance(url, str) and isinstance(number, int):
                        return HandoffResult(branch, url, number)
            has_next, cursor = _next_cursor(
                _mapping(repository.get("pullRequests", {}))
            )
            if not has_next:
                return None
            repository = None


class EnvironmentGitHubProvider:
    """Lazy provider used by the default FastAPI app."""

    def __init__(self) -> None:
        self._provider: ProjectProvider | None = None
        self._configuration: tuple[str | None, ...] | None = None
        self._lock = Lock()

    def _configured_provider(self) -> ProjectProvider:
        configuration = tuple(
            os.environ.get(name)
            for name in (
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "GITHUB_PROJECT_OWNER",
                "GITHUB_PROJECT_NUMBER",
                "GITHUB_PROJECT_OWNER_TYPE",
                DISCOVERY_CACHE_SECONDS_ENV,
            )
        )
        with self._lock:
            if self._provider is None or configuration != self._configuration:
                self._provider = GitHubProjectProvider.from_environment()
                self._configuration = configuration
            return self._provider

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return self._configured_provider().discover_project(project_id)

    def invalidate_discovery_cache(self) -> None:
        provider = self._configured_provider()
        invalidate = getattr(provider, "invalidate_discovery_cache", None)
        if callable(invalidate):
            invalidate()

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return self._configured_provider().create_handoff(request)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return self._configured_provider().resolve_base_branch(repository, requested)

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self._configured_provider().validate_handoff(
            repository, branch, requested_base
        )

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        return self._configured_provider().get_pull_request(repository, number)

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        self._configured_provider().update_source_branch(
            snapshot, worktree, expected_head, repaired_head
        )
