"""Provider boundaries for GitHub discovery and pull-request handoff."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote
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
from .pbi_creation import (
    PbiCreationError,
    PbiCreationProgress,
    PbiCreationProvider,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationScopeError,
    PbiCreationTarget,
    PbiCreationValidationError,
)
from .pbi_readiness import project_pbi_readiness
from .pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementMutationProvider,
    PbiRefinementTarget,
    PbiRefinementUpdateRequest,
    PbiRefinementUpdateResult,
    has_refined_pbi_sections,
    is_pbi_refinement_scale_label,
    merge_refinement_sections,
)
from .pbi_relations import (
    MAX_PBI_RELATION_CHILDREN,
    MAX_PBI_RELATION_GRAPH_ISSUES,
    PbiRelationIssue,
    PbiRelationProvider,
    PbiRelationProviderError,
    PbiRelationRequest,
    PbiRelationScopeError,
    PbiRelationSnapshot,
    PbiRelationValidationError,
)

PROVIDER_REQUEST_TIMEOUT = 30.0
DEFAULT_DISCOVERY_CACHE_SECONDS = 600.0
DISCOVERY_CACHE_SECONDS_ENV = "BEEHAIIVE_GITHUB_DISCOVERY_CACHE_SECONDS"
SECONDARY_RATE_LIMIT_FALLBACK_SECONDS = 60.0
HANDOFF_RATE_LIMIT_RETRIES = 2


class ProviderError(RuntimeError):
    """Raised when a provider cannot discover or hand off work."""


class GitHubOutcomeUnknownError(ProviderError):
    """Raised when a request may have reached GitHub without a usable reply."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(ProviderError):
    """Raised when GitHub asks the client to stop GraphQL requests temporarily."""

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        retry_after: float | None = None,
        primary: bool = False,
        remaining: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after
        self.primary = primary
        self.remaining = remaining


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
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _rate_limit_wait_seconds(
    error: GitHubRateLimitError,
    *,
    previous_secondary_wait: float | None,
) -> float:
    now = time.time()
    reset_at = (
        error.reset_at
        if error.reset_at is not None and math.isfinite(error.reset_at)
        else None
    )
    retry_after = (
        error.retry_after
        if error.retry_after is not None and math.isfinite(error.retry_after)
        else None
    )
    if error.primary:
        if reset_at is not None and reset_at > now:
            return max(0.0, reset_at - now)
        if retry_after is not None:
            return max(0.0, retry_after)
        return SECONDARY_RATE_LIMIT_FALLBACK_SECONDS

    if retry_after is not None:
        delay = max(0.0, retry_after)
    elif (
        error.remaining is not None
        and error.remaining <= 0
        and reset_at is not None
        and reset_at > now
    ):
        delay = max(0.0, reset_at - now)
    else:
        delay = SECONDARY_RATE_LIMIT_FALLBACK_SECONDS
    if previous_secondary_wait is not None:
        delay = max(delay, previous_secondary_wait * 2)
        if previous_secondary_wait == 0:
            delay = max(delay, SECONDARY_RATE_LIMIT_FALLBACK_SECONDS)
    return delay


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
        self._cooldown_remaining: float | None = None
        self._cooldown_primary = False

    def _raise_if_cooling_down(self) -> None:
        now = time.time()
        with self._cooldown_lock:
            if self._cooldown_until <= now:
                return
            reset_at = self._cooldown_reset_at or self._cooldown_until
            retry_after = self._cooldown_retry_after
            remaining = self._cooldown_remaining
            primary = self._cooldown_primary
        raise GitHubRateLimitError(
            "GitHub GraphQL rate limit cooldown is active",
            reset_at=reset_at,
            retry_after=retry_after,
            primary=primary,
            remaining=remaining,
        )

    def _set_cooldown(
        self,
        *,
        reset_at: float | None,
        retry_after: float | None,
        primary: bool,
        remaining: float | None,
    ) -> float:
        now = time.time()
        valid_reset_at = (
            reset_at
            if reset_at is not None and math.isfinite(reset_at) and reset_at > now
            else None
        )
        valid_retry_after = (
            retry_after
            if retry_after is not None and math.isfinite(retry_after)
            else None
        )
        if primary and valid_reset_at is not None:
            cooldown_until = valid_reset_at
        elif valid_retry_after is not None:
            cooldown_until = now + max(valid_retry_after, 0.0)
        elif remaining is not None and remaining <= 0 and valid_reset_at is not None:
            cooldown_until = valid_reset_at
        else:
            cooldown_until = now + SECONDARY_RATE_LIMIT_FALLBACK_SECONDS

        with self._cooldown_lock:
            if cooldown_until > self._cooldown_until:
                self._cooldown_until = cooldown_until
                self._cooldown_reset_at = valid_reset_at
                self._cooldown_retry_after = valid_retry_after
                self._cooldown_remaining = (
                    remaining
                    if remaining is not None and math.isfinite(remaining)
                    else None
                )
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
            remaining=remaining,
        )
        effective_reset_at = reset_at or cooldown_until
        kind = "primary" if primary else "secondary"
        return GitHubRateLimitError(
            f"GitHub GraphQL {kind} rate limit exceeded: {message}",
            reset_at=effective_reset_at,
            retry_after=retry_after,
            primary=primary,
            remaining=remaining,
        )

    def _record_exhausted_headers(self, response: object) -> None:
        remaining = _header_float(response, "x-ratelimit-remaining")
        if remaining is not None and remaining <= 0:
            self._set_cooldown(
                reset_at=_header_float(response, "x-ratelimit-reset"),
                retry_after=_header_float(response, "retry-after"),
                primary=True,
                remaining=remaining,
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
            message = f"GitHub GraphQL request failed: {exc}"
            if 500 <= exc.code < 600:
                raise GitHubOutcomeUnknownError(message, status_code=exc.code) from exc
            raise ProviderError(message) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub GraphQL returned invalid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub GraphQL request failed: {exc}"
            ) from exc

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

    def request_rest(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, Any] | list[Any]]:
        """Call a GitHub REST endpoint without hiding its response status."""

        request = Request(
            f"{self._endpoint.rsplit('/graphql', 1)[0]}{path}",
            data=(
                json.dumps(dict(payload)).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        response_headers: object | None = None
        try:
            with urlopen(request, timeout=PROVIDER_REQUEST_TIMEOUT) as response:
                response_headers = response
                status = int(getattr(response, "status", 200))
                raw_payload = response.read()
        except HTTPError as exc:
            rate_error = self._rate_limit_error(exc, status_code=exc.code)
            if rate_error is not None:
                raise rate_error from exc
            status = exc.code
            response_headers = exc
            raw_payload = exc.read()
            if status == 408 or status >= 500:
                raise GitHubOutcomeUnknownError(
                    f"GitHub REST request failed with HTTP {status}",
                    status_code=status,
                ) from exc
        except OSError as exc:
            raise GitHubOutcomeUnknownError(
                f"GitHub REST request failed: {exc}"
            ) from exc

        try:
            decoded: object = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubOutcomeUnknownError(
                "GitHub REST returned invalid JSON", status_code=status
            ) from exc
        if not isinstance(decoded, (Mapping, list)):
            raise GitHubOutcomeUnknownError(
                "GitHub REST returned an invalid response", status_code=status
            )
        self._record_exhausted_headers(response_headers)
        return status, cast(Mapping[str, Any] | list[Any], decoded)


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

    def complete_approved_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        """Merge one authorized PR and reconcile its persisted completion."""

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
              state
              stateReason
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
                  stateReason
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
          stateReason
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
      headRepository { nameWithOwner }
      headRef { name target { oid } }
      baseRefName
      baseRef { name target { oid } }
      mergeable
      mergeStateStatus
      isDraft
      mergeCommit { oid }
    }
  }
}
"""

ISSUE_COMPLETION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { id number state }
  }
}
"""

CLOSE_ISSUE_MUTATION = """
mutation($input: CloseIssueInput!) {
  closeIssue(input: $input) { issue { id number state } }
}
"""

BRANCH_REF_QUERY = """
query($owner: String!, $name: String!, $qualifiedName: String!) {
  repository(owner: $owner, name: $name) {
    id
    ref(qualifiedName: $qualifiedName) { id name target { oid } }
  }
}
"""

UPDATE_REFS_MUTATION = """
mutation($input: UpdateRefsInput!) {
  updateRefs(input: $input) { clientMutationId }
}
"""

COMPLETION_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name options { id name }
          }
        }
      }
      items(first: 100, after: $cursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue { number repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name optionId
                field { ... on ProjectV2FieldCommon { id name } }
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

UPDATE_PROJECT_STATUS_MUTATION = """
mutation($input: UpdateProjectV2ItemFieldValueInput!) {
  updateProjectV2ItemFieldValue(input: $input) {
    projectV2Item { id }
  }
}
"""

PBI_CREATION_TARGET_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      repositories(first: 100, after: $cursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name options { id name }
          }
        }
      }
    }
  }
}
"""

PBI_CREATION_LABELS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
    labels(first: 100, after: $cursor) {
      nodes { id name description }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PBI_CREATION_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id number url title body state
      labels(first: 100) { nodes { name } }
    }
  }
}
"""

PBI_CREATION_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name options { id name }
          }
        }
      }
      items(first: 100, after: $cursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue { id number repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name optionId
                field { ... on ProjectV2FieldCommon { id name } }
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

PBI_RELATION_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $repositoryCursor: String, $itemCursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      repositories(first: 100, after: $repositoryCursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
      fields(first: 100) {
        nodes { ... on ProjectV2SingleSelectField { id name } }
      }
      items(first: 100, after: $itemCursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue { id number repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { id name } }
              }
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

PBI_REFINEMENT_ISSUE_QUERY = """
query PbiRefinementIssue($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id number url body state
      labels(first: 100) {
        nodes { name }
        pageInfo { hasNextPage endCursor }
      }
      subIssues(first: 100) {
        nodes { number title state }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

UPDATE_REFINEMENT_ISSUE_MUTATION = """
mutation UpdatePbiRefinementIssue($input: UpdateIssueInput!) {
  updateIssue(input: $input) { issue { id } }
}
"""

ADD_PROJECT_ITEM_MUTATION = """
mutation($input: AddProjectV2ItemByIdInput!) {
  addProjectV2ItemById(input: $input) { item { id } }
}
"""

ADD_ISSUE_LABELS_MUTATION = """
mutation($input: AddLabelsToLabelableInput!) {
  addLabelsToLabelable(input: $input) {
    labelable { ... on Issue { id labels(first: 100) { nodes { name } } } }
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
    ref(qualifiedName: $qualifiedBranch) { name target { oid } }
    pullRequests(
      first: 100
      after: $pullRequestCursor
      states: [OPEN, CLOSED, MERGED]
    ) {
      nodes {
        id number url title body state isDraft
        headRefName headRefOid baseRefName
      }
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
  createRef(input: $input) { ref { name target { oid } } }
}
"""

CREATE_PULL_REQUEST_MUTATION = """
mutation($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest {
      id number url title body state isDraft
      headRefName headRefOid baseRefName
    }
  }
}
"""

UPDATE_PULL_REQUEST_MUTATION = """
mutation($input: UpdatePullRequestInput!) {
  updatePullRequest(input: $input) {
    pullRequest {
      id number url title body state isDraft
      headRefName headRefOid baseRefName
    }
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
    is_draft_value = pull_request.get("isDraft")
    is_draft = is_draft_value if isinstance(is_draft_value, bool) else None
    raw_head_repository = pull_request.get("headRepository")
    source_repository = (
        _required_text(
            cast(Mapping[str, Any], raw_head_repository).get("nameWithOwner"),
            "source repository",
        )
        if isinstance(raw_head_repository, Mapping)
        else None
    )
    raw_merge_commit = pull_request.get("mergeCommit")
    merge_commit_oid = (
        _commit_oid(cast(Mapping[str, Any], raw_merge_commit).get("oid"))
        if isinstance(raw_merge_commit, Mapping)
        else None
    )

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
            is_draft,
            merge_commit_oid,
            source_repository,
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
        None,
        is_draft,
        merge_commit_oid,
        source_repository,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError("GitHub GraphQL returned an invalid object")
    return cast(Mapping[str, Any], value)


def require_graphql_mapping(value: object) -> Mapping[str, Any]:
    """Validate and narrow an object returned by the GitHub GraphQL API."""

    return _mapping(value)


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


def _next_cursor(
    connection: Mapping[str, Any],
    seen_cursors: set[str] | None = None,
    *,
    strict: bool = False,
) -> tuple[bool, str | None]:
    if strict:
        nodes_value: object = connection.get("nodes")
        if not isinstance(nodes_value, list) or not all(
            isinstance(node, Mapping) for node in cast(list[object], nodes_value)
        ):
            raise ProviderError("GitHub GraphQL connection returned invalid nodes")
        page_info_value: object = connection.get("pageInfo")
        if not isinstance(page_info_value, Mapping) or not isinstance(
            cast(Mapping[str, Any], page_info_value).get("hasNextPage"), bool
        ):
            raise ProviderError("GitHub GraphQL connection omitted pagination state")
    page_info = _mapping(connection.get("pageInfo", {}))
    has_next = page_info.get("hasNextPage") is True
    cursor = page_info.get("endCursor")
    if has_next and (not isinstance(cursor, str) or not cursor):
        raise ProviderError("GitHub GraphQL page did not include an end cursor")
    if has_next and isinstance(cursor, str) and seen_cursors is not None:
        if cursor in seen_cursors:
            raise ProviderError("GitHub GraphQL pagination repeated a cursor")
        seen_cursors.add(cursor)
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
    *,
    strict: bool = False,
) -> dict[str, Any]:
    def validated_connection(value: object) -> Mapping[str, Any]:
        connection = _mapping(value)
        if strict:
            _next_cursor(connection, strict=True)
        return connection

    connection = validated_connection(initial)
    nodes = list(_nodes(connection))
    seen_cursors: set[str] = set()
    has_next, cursor = _next_cursor(connection, seen_cursors)
    while has_next:
        assert cursor is not None
        page_data = client.execute(
            query,
            {**variables, "cursor": cursor},
        )
        page = validated_connection(_connection_at(page_data, response_path))
        nodes.extend(_nodes(page))
        has_next, cursor = _next_cursor(page, seen_cursors)
    completed = dict(connection)
    completed["nodes"] = nodes
    completed["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    return completed


def complete_graphql_connection(
    client: GraphQLClient,
    initial: object,
    query: str,
    variables: Mapping[str, object],
    response_path: tuple[str, ...],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Complete a GraphQL connection, optionally rejecting incomplete pages."""

    return _complete_connection(
        client, initial, query, variables, response_path, strict=strict
    )


def _stage_from_status(status: str | None) -> Stage | None:
    return project_stage_from_status(status)


def _project_item_status_values(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for field_value in _nodes(item.get("fieldValues", {})):
        raw_field = field_value.get("field")
        if raw_field is None:
            continue
        field = _mapping(raw_field)
        value = field_value.get("name")
        if field.get("name") == "Status" and isinstance(value, str):
            values.append(value)
    return values


def _project_status_index(
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], str | None], set[tuple[str, int]]]:
    statuses: dict[tuple[str, int], str | None] = {}
    conflicts: set[tuple[str, int]] = set()
    for item in items:
        content_value = item.get("content")
        if content_value is None:
            continue
        content = _mapping(content_value)
        repository_value = content.get("repository")
        if repository_value is None:
            continue
        repository_name = _mapping(repository_value).get("nameWithOwner")
        number = content.get("number")
        if not isinstance(repository_name, str) or not isinstance(number, int):
            continue
        key = (repository_name.casefold(), number)
        values = set(_project_item_status_values(item))
        status = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1 or (key in statuses and statuses[key] != status):
            conflicts.add(key)
        statuses[key] = status
    return statuses, conflicts


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
    issue_state = issue.get("state")
    if isinstance(issue_state, str):
        metadata["issue_state"] = issue_state
    state_reason = issue.get("stateReason")
    if isinstance(state_reason, str):
        metadata["state_reason"] = state_reason
    project_status = issue.get("projectStatus")
    if isinstance(project_status, str):
        metadata["project_status"] = project_status
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
            subtask["issue_state"] = state
        state_reason = raw_subtask.get("stateReason")
        if isinstance(state_reason, str):
            subtask["state_reason"] = state_reason
        child_project_status = raw_subtask.get("projectStatus")
        if isinstance(child_project_status, str):
            subtask["project_status"] = child_project_status
        else:
            subtask["project_status"] = None
        subtask["project_status_conflict"] = (
            raw_subtask.get("projectStatusConflict") is True
        )
        subtask["blocked_by"] = raw_subtask.get("blockedBy", [])
        subtask["dependency_read_complete"] = (
            raw_subtask.get("dependencyReadComplete") is True
        )
        dependency_read_error = raw_subtask.get("dependencyReadError")
        if isinstance(dependency_read_error, str):
            subtask["dependency_read_error"] = dependency_read_error
        subtask_labels = _label_names(raw_subtask.get("labels", {}))
        if subtask_labels:
            subtask["labels"] = subtask_labels
        subtasks.append(subtask)
    readiness_source = _mapping(issue.get("_pbi_readiness_source", {}))
    is_epic = any(label.casefold() == "effort 13 - epic" for label in labels)
    if subtasks or is_epic:
        projected_subtasks, readiness = project_pbi_readiness(
            subtasks,
            relation_complete=readiness_source.get("child_relation_complete") is True,
            relation_error=(
                readiness_source.get("child_relation_error")
                if isinstance(readiness_source.get("child_relation_error"), str)
                else None
            ),
            observed_at=(
                readiness_source.get("observed_at")
                if isinstance(readiness_source.get("observed_at"), str)
                else None
            ),
        )
        metadata["subtasks"] = projected_subtasks
        metadata["dependency_readiness"] = readiness

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


def _handoff_marker(request: HandoffRequest, base_branch: str | None = None) -> str:
    if not request.run_id.strip():
        raise ProviderError("Handoff run identity is required")
    payload = json.dumps(
        {
            "base_branch": (
                request.base_branch if base_branch is None else base_branch
            ),
            "body_intent_sha256": hashlib.sha256(
                request.body.encode("utf-8")
            ).hexdigest(),
            "branch": request.branch,
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


def _begin_handoff_mutation(
    request: HandoffRequest,
    mutation: str,
    operation_key: str,
    target: Mapping[str, object],
) -> str | None:
    if request.mutation_audit is None:
        return None
    return request.mutation_audit.begin_handoff_mutation(
        request, mutation, operation_key, target
    )


def _finish_handoff_mutation(
    request: HandoffRequest,
    action_id: str | None,
    status: str,
    result: Mapping[str, object],
) -> None:
    if action_id is not None and request.mutation_audit is not None:
        request.mutation_audit.finish_handoff_mutation(action_id, status, result)


def _reconcile_handoff_mutation(
    request: HandoffRequest,
    mutation: str,
    operation_key: str,
    status: str,
    result: Mapping[str, object],
) -> None:
    if request.mutation_audit is not None:
        request.mutation_audit.reconcile_handoff_mutation(
            request, mutation, operation_key, status, result
        )


def _mutation_error_result(error: Exception) -> dict[str, object]:
    result: dict[str, object] = {"error_class": type(error).__name__}
    if isinstance(error, GitHubRateLimitError):
        rate_limit: dict[str, object] = {
            "classification": "primary" if error.primary else "secondary"
        }
        if error.reset_at is not None and math.isfinite(error.reset_at):
            rate_limit["reset_at"] = error.reset_at
        if error.retry_after is not None and math.isfinite(error.retry_after):
            rate_limit["retry_after"] = error.retry_after
        if error.remaining is not None and math.isfinite(error.remaining):
            rate_limit["remaining"] = error.remaining
        result["rate_limit"] = rate_limit
    return result


def _pull_request_audit_result(
    pull_request: Mapping[str, Any], reconciliation: str
) -> dict[str, object]:
    result: dict[str, object] = {"reconciliation": reconciliation}
    for source, target in (
        ("id", "pull_request_id"),
        ("number", "pull_request_number"),
        ("url", "pull_request_url"),
    ):
        value = pull_request.get(source)
        if (source == "number" and type(value) is int) or (
            source != "number" and isinstance(value, str)
        ):
            result[target] = value
    return result


def _legacy_handoff_marker(request: HandoffRequest) -> str:
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


def _handoff_body(body: str, marker: str, request: HandoffRequest | None = None) -> str:
    if marker in body and request is None:
        return body
    body_without_marker = body.replace(marker, "").rstrip()
    sections = [body_without_marker] if body_without_marker.strip() else []
    if request is not None:
        issue_url = (
            f"https://github.com/{request.repository}/issues/{request.pbi_number}"
        )
        sections.extend(
            (
                f"PBI: [#{request.pbi_number}]({issue_url})",
                f"Run: `{request.run_id}`",
            )
        )
        if request.head_sha:
            sections.append(f"Pushed head: `{request.head_sha}`")
        if request.verification_evidence:
            sections.extend(("Verification evidence:", request.verification_evidence))
    sections.append(marker)
    return "\n\n".join(sections)


def _pull_request_matches(
    pull_request: Mapping[str, Any],
    branch: str,
    base_branch: str,
    identity_marker: str,
    legacy_marker: str | None = None,
    legacy_body: str | None = None,
) -> bool:
    body = pull_request.get("body")
    return (
        pull_request.get("headRefName") == branch
        and pull_request.get("baseRefName") == base_branch
        and isinstance(body, str)
        and (
            identity_marker in body
            or (
                legacy_marker is not None
                and legacy_body is not None
                and body == legacy_body
                and legacy_marker in body
            )
        )
    )


def _branch_ref_matches(
    branch_ref: Mapping[str, Any], qualified_branch: str, expected_oid: str
) -> bool:
    if branch_ref.get("name") != qualified_branch:
        return False
    target = branch_ref.get("target")
    if not isinstance(target, Mapping):
        return False
    branch_oid = cast(Mapping[str, Any], target).get("oid")
    return isinstance(branch_oid, str) and branch_oid.lower() == expected_oid.lower()


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

    def _complete_issue_metadata(
        self,
        issue: Mapping[str, Any],
        project_statuses: Mapping[tuple[str, int], str | None] | None = None,
        project_status_conflicts: set[tuple[str, int]] | None = None,
    ) -> Mapping[str, Any]:
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
        child_relation_complete = True
        child_relation_error: str | None = None
        try:
            subissues = _complete_connection(
                self._client,
                issue.get("subIssues", {}),
                ISSUE_SUB_ISSUES_QUERY,
                variables,
                ("repository", "issue", "subIssues"),
                strict=True,
            )
        except (OSError, TypeError, ValueError, ProviderError):
            subissues = dict(_mapping(issue.get("subIssues", {})))
            child_relation_complete = False
            child_relation_error = "child_relation_read_failed"
        if len(_nodes(subissues)) > MAX_PBI_RELATION_CHILDREN:
            child_relation_complete = False
            child_relation_error = "child_limit_exceeded"
        completed_subissues: list[dict[str, Any]] = []
        for index, raw_subissue in enumerate(_nodes(subissues)):
            subissue = dict(raw_subissue)
            subissue_number = subissue.get("number")
            subissue_title = subissue.get("title")
            if (
                not isinstance(subissue_number, int)
                or subissue_number <= 0
                or not isinstance(subissue_title, str)
                or not subissue_title.strip()
            ):
                child_relation_complete = False
                child_relation_error = "child_response_incomplete"
            else:
                child_key = (repository_name.casefold(), subissue_number)
                subissue["projectStatus"] = (
                    project_statuses.get(child_key)
                    if project_statuses is not None
                    else None
                )
                subissue["projectStatusConflict"] = (
                    project_status_conflicts is not None
                    and child_key in project_status_conflicts
                )
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
                if not child_relation_complete:
                    subissue["dependencyReadComplete"] = False
                    subissue["dependencyReadError"] = (
                        child_relation_error or "child_relation_incomplete"
                    )
                elif index >= MAX_PBI_RELATION_CHILDREN:
                    subissue["dependencyReadComplete"] = False
                    subissue["dependencyReadError"] = "child_limit_exceeded"
                else:
                    try:
                        blockers = self.list_pbi_blocked_by(
                            repository_name, subissue_number
                        )
                    except PbiRelationProviderError as exc:
                        subissue["dependencyReadComplete"] = False
                        subissue["dependencyReadError"] = exc.code
                    except (OSError, TypeError, ValueError, ProviderError):
                        subissue["dependencyReadComplete"] = False
                        subissue["dependencyReadError"] = "dependency_read_failed"
                    else:
                        subissue["blockedBy"] = [
                            {
                                "number": blocker.number,
                                "title": blocker.title,
                                "url": blocker.url,
                                "state": blocker.state,
                                "state_reason": blocker.state_reason,
                            }
                            for blocker in blockers
                        ]
                        subissue["dependencyReadComplete"] = True
            completed_subissues.append(subissue)
        subissues["nodes"] = completed_subissues
        completed_issue["subIssues"] = subissues
        completed_issue["_pbi_readiness_source"] = {
            "child_relation_complete": child_relation_complete,
            "child_relation_error": child_relation_error,
            "observed_at": datetime.now(UTC).isoformat(),
        }
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

        project_items: list[Mapping[str, Any]] = []
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
            project_items.extend(_nodes(item_connection))
            has_next, item_cursor = _next_cursor(item_connection)
            if not has_next:
                break

        project_statuses, project_status_conflicts = _project_status_index(
            project_items
        )
        for item in project_items:
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
            key = (repository_name.casefold(), number)
            status_values = set(_project_item_status_values(item))
            status = next(iter(status_values)) if len(status_values) == 1 else None
            content_with_project_status = dict(content)
            content_with_project_status["projectStatus"] = status
            content_with_project_status["projectStatusConflict"] = (
                len(status_values) > 1 or key in project_status_conflicts
            )
            completed_content = self._complete_issue_metadata(
                content_with_project_status,
                project_statuses,
                project_status_conflicts,
            )
            stage = _stage_from_status(status)
            repositories.setdefault(repository_name, []).append(
                PbiSnapshot(
                    repository_name,
                    number,
                    title,
                    stage,
                    status,
                    stage is not None,
                    _dashboard_metadata(completed_content),
                )
            )

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

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        """Validate the live Project, linked repository, labels, and Backlog option."""

        if request.project_id != self.project_id:
            raise PbiCreationScopeError("Project is not authorized")
        repository_owner, repository_name = self._repository_parts(request.repository)
        cursor: str | None = None
        project_node_id: str | None = None
        linked_repositories: set[str] = set()
        status_field_id: str | None = None
        backlog_option_id: str | None = None
        backlog_status: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_TARGET_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiCreationScopeError("Configured Project is unavailable")
            if project_node_id is not None and current_project_id != project_node_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_node_id = current_project_id

            repository_connection = _mapping(project.get("repositories"))
            for raw_repository in _nodes(repository_connection):
                name_with_owner = raw_repository.get("nameWithOwner")
                if isinstance(name_with_owner, str):
                    linked_repositories.add(name_with_owner.casefold())

            status_fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(status_fields) != 1:
                raise PbiCreationValidationError(
                    "Configured Project must have one Status field",
                    code="project_status_unavailable",
                )
            status_field = status_fields[0]
            raw_status_field_id = status_field.get("id")
            options = status_field.get("options")
            if not isinstance(raw_status_field_id, str) or not isinstance(
                options, list
            ):
                raise PbiCreationValidationError(
                    "Configured Project Status field is incomplete",
                    code="project_status_unavailable",
                )
            backlog_options: list[Mapping[str, object]] = []
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                option_name = option.get("name")
                if isinstance(option_name, str) and option_name.casefold() == "backlog":
                    backlog_options.append(option)
            if len(backlog_options) != 1:
                raise PbiCreationValidationError(
                    "Configured Project must have one Backlog Status option",
                    code="project_backlog_unavailable",
                )
            raw_backlog_option_id = backlog_options[0].get("id")
            raw_backlog_status = backlog_options[0].get("name")
            if not isinstance(raw_backlog_option_id, str) or not isinstance(
                raw_backlog_status, str
            ):
                raise PbiCreationValidationError(
                    "Configured Project Backlog option is incomplete",
                    code="project_backlog_unavailable",
                )
            if status_field_id is not None and status_field_id != raw_status_field_id:
                raise ProviderError("GitHub returned conflicting Status fields")
            if (
                backlog_option_id is not None
                and backlog_option_id != raw_backlog_option_id
            ):
                raise ProviderError("GitHub returned conflicting Backlog options")
            status_field_id = raw_status_field_id
            backlog_option_id = raw_backlog_option_id
            backlog_status = raw_backlog_status

            has_next, cursor = _next_cursor(repository_connection)
            if not has_next:
                break

        if request.repository.casefold() not in linked_repositories:
            raise PbiCreationScopeError(
                "Repository is not linked to the configured Project"
            )
        label_cursor: str | None = None
        repository_node_id: str | None = None
        labels_by_name: dict[str, str] = {}
        while True:
            label_data = self._client.execute(
                PBI_CREATION_LABELS_QUERY,
                {
                    "owner": repository_owner,
                    "name": repository_name,
                    "cursor": label_cursor,
                },
            )
            repository = _mapping(label_data.get("repository"))
            current_repository_id = repository.get("id")
            current_repository_name = repository.get("nameWithOwner")
            if (
                not isinstance(current_repository_id, str)
                or not current_repository_id
                or not isinstance(current_repository_name, str)
                or current_repository_name.casefold() != request.repository.casefold()
            ):
                raise PbiCreationScopeError("Repository is not available")
            if (
                repository_node_id is not None
                and repository_node_id != current_repository_id
            ):
                raise ProviderError("GitHub returned conflicting repository identities")
            repository_node_id = current_repository_id
            label_connection = _mapping(repository.get("labels"))
            for label in _nodes(label_connection):
                name = label.get("name")
                label_id = label.get("id")
                if isinstance(name, str) and isinstance(label_id, str):
                    labels_by_name[name] = label_id
            has_next, label_cursor = _next_cursor(label_connection)
            if not has_next:
                break

        missing_labels = [name for name in request.labels if name not in labels_by_name]
        if missing_labels:
            raise PbiCreationValidationError(
                "One or more labels do not exist in the target repository",
                code="unknown_label",
            )
        return PbiCreationTarget(
            project_node_id=project_node_id,
            repository_node_id=repository_node_id,
            status_field_id=status_field_id,
            backlog_option_id=backlog_option_id,
            backlog_status=backlog_status,
            label_ids=tuple(labels_by_name[name] for name in request.labels),
        )

    def _prepare_pbi_refinement_target(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementTarget:
        if request.project_id != self.project_id:
            raise PbiRefinementMutationError(
                "Project is not authorized",
                code="project_not_authorized",
                status_code=403,
            )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        project_node_id: str | None = None
        repository_names: set[str] = set()
        status_field_id: str | None = None
        status_options: dict[str, tuple[str, str]] = {}
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_TARGET_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiRefinementMutationError(
                    "Configured Project is unavailable",
                    code="project_unavailable",
                    status_code=403,
                )
            if project_node_id is not None and current_project_id != project_node_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_node_id = current_project_id

            repositories = _mapping(project.get("repositories"))
            for repository in _nodes(repositories):
                name = repository.get("nameWithOwner")
                if isinstance(name, str):
                    repository_names.add(name.casefold())

            fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(fields) != 1:
                raise PbiRefinementMutationError(
                    "Configured Project must have one Status field",
                    code="project_status_unavailable",
                )
            field_id = fields[0].get("id")
            options = fields[0].get("options")
            if not isinstance(field_id, str) or not isinstance(options, list):
                raise PbiRefinementMutationError(
                    "Configured Project Status field is incomplete",
                    code="project_status_unavailable",
                )
            if status_field_id is not None and status_field_id != field_id:
                raise ProviderError("GitHub returned conflicting Status fields")
            status_field_id = field_id

            found_options: dict[str, list[tuple[str, str]]] = {
                "backlog": [],
                "todo": [],
            }
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                name = option.get("name")
                option_id = option.get("id")
                if (
                    isinstance(name, str)
                    and name.casefold() in found_options
                    and isinstance(option_id, str)
                    and option_id
                ):
                    found_options[name.casefold()].append((option_id, name))
            if any(len(matches) != 1 for matches in found_options.values()):
                raise PbiRefinementMutationError(
                    "Configured Project must have unique Backlog and Todo options",
                    code="project_status_options_unavailable",
                )
            page_options = {name: matches[0] for name, matches in found_options.items()}
            if status_options and status_options != page_options:
                raise ProviderError("GitHub returned conflicting Status options")
            status_options = page_options

            has_next, cursor = _next_cursor(repositories, seen_cursors)
            if not has_next:
                break

        if request.repository.casefold() not in repository_names:
            raise PbiRefinementMutationError(
                "Repository is not linked to the configured Project",
                code="repository_not_linked",
                status_code=403,
            )
        backlog_id, backlog_name = status_options["backlog"]
        todo_id, todo_name = status_options["todo"]
        return PbiRefinementTarget(
            project_node_id=project_node_id,
            status_field_id=status_field_id,
            backlog_option_id=backlog_id,
            backlog_status=backlog_name,
            todo_option_id=todo_id,
            todo_status=todo_name,
        )

    def _pbi_refinement_repository_labels(
        self, request: PbiRefinementUpdateRequest
    ) -> dict[str, list[Mapping[str, Any]]]:
        owner, name = self._repository_parts(request.repository)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        repository_id: str | None = None
        labels: dict[str, list[Mapping[str, Any]]] = {}
        while True:
            data = self._client.execute(
                PBI_CREATION_LABELS_QUERY,
                {"owner": owner, "name": name, "cursor": cursor},
            )
            repository = _mapping(data.get("repository"))
            current_id = repository.get("id")
            current_name = repository.get("nameWithOwner")
            if (
                not isinstance(current_id, str)
                or not current_id
                or not isinstance(current_name, str)
                or current_name.casefold() != request.repository.casefold()
            ):
                raise PbiRefinementMutationError(
                    "Repository is not available",
                    code="repository_unavailable",
                    status_code=403,
                )
            if repository_id is not None and repository_id != current_id:
                raise ProviderError("GitHub returned conflicting repository identities")
            repository_id = current_id
            connection = _mapping(repository.get("labels"))
            for label in _nodes(connection):
                label_name = label.get("name")
                if isinstance(label_name, str):
                    labels.setdefault(label_name.casefold(), []).append(label)
            has_next, cursor = _next_cursor(connection, seen_cursors)
            if not has_next:
                break
        return labels

    def _pbi_refinement_issue_state(
        self, request: PbiRefinementUpdateRequest
    ) -> Mapping[str, Any]:
        owner, name = self._repository_parts(request.repository)
        variables = {"owner": owner, "name": name, "number": request.pbi_number}
        data = self._client.execute(PBI_REFINEMENT_ISSUE_QUERY, variables)
        issue = _mapping(_mapping(data.get("repository")).get("issue"))
        if not issue:
            raise PbiRefinementMutationError(
                "Issue was not found in the configured repository",
                code="issue_not_found",
                status_code=404,
            )
        if issue.get("number") != request.pbi_number:
            raise PbiRefinementMutationError(
                "GitHub returned a different issue",
                code="issue_identity_mismatch",
                status_code=409,
            )
        completed = dict(issue)
        completed["labels"] = _complete_connection(
            self._client,
            issue.get("labels"),
            ISSUE_LABELS_QUERY,
            variables,
            ("repository", "issue", "labels"),
            strict=True,
        )
        completed["subIssues"] = _complete_connection(
            self._client,
            issue.get("subIssues"),
            ISSUE_SUB_ISSUES_QUERY,
            variables,
            ("repository", "issue", "subIssues"),
            strict=True,
        )
        return completed

    def _pbi_refinement_project_item(
        self,
        request: PbiRefinementUpdateRequest,
        target: PbiRefinementTarget,
        issue: Mapping[str, Any],
    ) -> dict[str, object]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matches: list[dict[str, object]] = []
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_PROJECT_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            if project.get("id") != target.project_node_id:
                raise PbiRefinementMutationError(
                    "Configured Project identity changed",
                    code="project_identity_changed",
                    status_code=409,
                )
            status_fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(status_fields) != 1:
                raise PbiRefinementMutationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                    status_code=409,
                )
            status_field = status_fields[0]
            field_id = status_field.get("id")
            options = status_field.get("options")
            if field_id != target.status_field_id or not isinstance(options, list):
                raise PbiRefinementMutationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                    status_code=409,
                )
            status_options: dict[str, list[tuple[str, str]]] = {
                "backlog": [],
                "todo": [],
            }
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                option_name = option.get("name")
                option_id = option.get("id")
                if (
                    isinstance(option_name, str)
                    and option_name.casefold() in status_options
                    and isinstance(option_id, str)
                ):
                    status_options[option_name.casefold()].append(
                        (option_id, option_name)
                    )
            if (
                len(status_options["backlog"]) != 1
                or status_options["backlog"][0]
                != (target.backlog_option_id, target.backlog_status)
                or len(status_options["todo"]) != 1
                or status_options["todo"][0]
                != (target.todo_option_id, target.todo_status)
            ):
                raise PbiRefinementMutationError(
                    "Configured Project Status options changed",
                    code="project_status_options_changed",
                    status_code=409,
                )

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                raw_content = raw_item.get("content")
                if not isinstance(raw_content, Mapping):
                    continue
                content = cast(Mapping[str, Any], raw_content)
                repository = _mapping(content.get("repository")).get("nameWithOwner")
                if (
                    content.get("__typename") != "Issue"
                    or content.get("number") != request.pbi_number
                    or not isinstance(repository, str)
                    or repository.casefold() != request.repository.casefold()
                    or content.get("id") != issue.get("id")
                ):
                    continue
                values = [
                    value
                    for value in _nodes(raw_item.get("fieldValues"))
                    if _mapping(value.get("field")).get("id") == target.status_field_id
                ]
                if len(values) != 1:
                    raise PbiRefinementMutationError(
                        "PBI Project Status is missing or ambiguous",
                        code="project_item_status_unavailable",
                        status_code=409,
                    )
                value = values[0]
                status = value.get("name")
                option_id = value.get("optionId")
                if not isinstance(status, str) or not isinstance(option_id, str):
                    raise PbiRefinementMutationError(
                        "PBI Project Status is incomplete",
                        code="project_item_status_unavailable",
                        status_code=409,
                    )
                expected_ids = {
                    target.backlog_status: target.backlog_option_id,
                    target.todo_status: target.todo_option_id,
                }
                if expected_ids.get(status) != option_id:
                    raise PbiRefinementMutationError(
                        "PBI has an unknown Project Status option",
                        code="project_item_status_unknown",
                        status_code=409,
                    )
                item_id = raw_item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise PbiRefinementMutationError(
                        "PBI Project item identity is incomplete",
                        code="project_item_identity_unavailable",
                        status_code=409,
                    )
                matches.append({"item_id": item_id, "status": status})
            has_next, cursor = _next_cursor(items, seen_cursors, strict=True)
            if not has_next:
                break

        if len(matches) != 1:
            raise PbiRefinementMutationError(
                "PBI must appear exactly once in the configured Project",
                code="project_membership_unconfirmed",
                status_code=409,
            )
        return matches[0]

    def apply_pbi_refinement(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult:
        request.validate()
        try:
            target = self._prepare_pbi_refinement_target(request)
            labels_by_name = self._pbi_refinement_repository_labels(request)
            issue = self._pbi_refinement_issue_state(request)
            project_item = self._pbi_refinement_project_item(request, target, issue)
        except PbiRefinementMutationError:
            raise
        except Exception as exc:
            raise PbiRefinementMutationError(
                "GitHub refinement preflight failed",
                code="preflight_failed",
                status_code=502,
            ) from exc

        if issue.get("state") != "OPEN":
            raise PbiRefinementMutationError(
                "Only an open issue can be refined",
                code="issue_not_open",
                status_code=409,
            )
        issue_url = issue.get("url")
        issue_id = issue.get("id")
        if (
            not isinstance(issue_url, str)
            or not issue_url
            or not isinstance(issue_id, str)
        ):
            raise PbiRefinementMutationError(
                "GitHub issue identity is incomplete",
                code="issue_identity_unavailable",
                status_code=409,
            )

        initial_sub_issues = self._pbi_refinement_sub_issues(issue)
        is_epic = request.effort_label.casefold() == "effort 13 - epic"
        if is_epic and not initial_sub_issues:
            raise PbiRefinementMutationError(
                "Effort 13 Epic requires linked split evidence",
                code="missing_split_evidence",
                status_code=409,
            )
        desired_status = target.backlog_status if is_epic else target.todo_status
        allowed_statuses = {
            target.backlog_status.casefold(),
            target.todo_status.casefold(),
        }
        project_status = project_item.get("status")
        if not isinstance(project_status, str):
            raise PbiRefinementMutationError(
                "PBI Project Status is incomplete",
                code="project_item_status_unavailable",
                status_code=409,
            )
        if project_status.casefold() not in allowed_statuses:
            raise PbiRefinementMutationError(
                "PBI Project Status changed outside Backlog and Todo",
                code="project_status_conflict",
                status_code=409,
            )

        current_body = issue.get("body")
        if current_body is None:
            current_body = ""
        if not isinstance(current_body, str):
            raise PbiRefinementMutationError(
                "GitHub issue body is invalid",
                code="issue_body_unavailable",
                status_code=409,
            )
        try:
            desired_body = merge_refinement_sections(current_body, request.sections)
            actual_label_names = self._pbi_refinement_label_names(issue)
            resolved_labels = [
                self._resolve_pbi_refinement_label(
                    labels_by_name, label, require_description=True
                )
                for label in (
                    request.priority_label,
                    request.effort_label,
                    *request.standard_labels,
                )
            ]
            preserved_labels = [
                self._resolve_pbi_refinement_label(
                    labels_by_name, label, require_description=False
                )
                for label in actual_label_names
                if not is_pbi_refinement_scale_label(label)
            ]
        except PbiRefinementMutationError:
            raise
        except Exception as exc:
            raise PbiRefinementMutationError(
                "Issue labels or body are invalid",
                code="issue_state_invalid",
                status_code=409,
            ) from exc

        expected_labels_by_id = {
            label_id: label_name
            for label_id, label_name in (*preserved_labels, *resolved_labels)
        }
        expected_label_ids = sorted(expected_labels_by_id)
        expected_label_names = {
            name.casefold() for name in expected_labels_by_id.values()
        }
        initial_label_names = {name.casefold() for name in actual_label_names}
        completed_steps: list[str] = []

        def result(
            status: Literal["complete", "partial"],
            current_issue: Mapping[str, Any],
            current_project: Mapping[str, object],
            *,
            pending_step: str | None = None,
            failure_code: str | None = None,
        ) -> PbiRefinementUpdateResult:
            try:
                readback_labels = tuple(
                    sorted(
                        self._pbi_refinement_label_names(current_issue),
                        key=str.casefold,
                    )
                )
            except PbiRefinementMutationError:
                readback_labels = ()
            try:
                linked_sub_issues = self._pbi_refinement_sub_issues(current_issue)
            except PbiRefinementMutationError:
                linked_sub_issues = initial_sub_issues
            return PbiRefinementUpdateResult(
                status=status,
                issue_number=request.pbi_number,
                issue_url=issue_url,
                labels=readback_labels,
                project_item_id=str(current_project.get("item_id", "")),
                project_status=str(current_project.get("status", "")),
                linked_sub_issues=linked_sub_issues,
                completed_steps=tuple(completed_steps),
                pending_step=pending_step,
                failure_code=failure_code,
            )

        needs_issue_update = (
            current_body != desired_body or initial_label_names != expected_label_names
        )
        if needs_issue_update:
            try:
                current_issue = self._pbi_refinement_issue_state(request)
            except Exception:
                return result(
                    "partial",
                    issue,
                    project_item,
                    pending_step="issue_body_and_labels",
                    failure_code="issue_prewrite_readback_failed",
                )
            current_issue_body = current_issue.get("body")
            if current_issue_body is None:
                current_issue_body = ""
            if (
                current_issue.get("state") != "OPEN"
                or current_issue_body != current_body
                or {
                    name.casefold()
                    for name in self._pbi_refinement_label_names(current_issue)
                }
                != initial_label_names
                or self._pbi_refinement_sub_issues(current_issue) != initial_sub_issues
            ):
                raise PbiRefinementMutationError(
                    "Issue changed during refinement validation; "
                    "retry against live state",
                    code="issue_changed_before_write",
                    status_code=409,
                )
            try:
                self._client.execute(
                    UPDATE_REFINEMENT_ISSUE_MUTATION,
                    {
                        "input": {
                            "id": issue_id,
                            "body": desired_body,
                            "labelIds": expected_label_ids,
                        }
                    },
                )
            except Exception:
                try:
                    current_issue = self._pbi_refinement_issue_state(request)
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_update_unconfirmed",
                    )
                if not self._pbi_refinement_issue_matches(
                    current_issue,
                    desired_body,
                    expected_label_names,
                    initial_sub_issues,
                ):
                    return result(
                        "partial",
                        current_issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_update_unconfirmed",
                    )
                issue = current_issue
            else:
                try:
                    issue = self._pbi_refinement_issue_state(request)
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_readback_failed",
                    )
                if not self._pbi_refinement_issue_matches(
                    issue, desired_body, expected_label_names, initial_sub_issues
                ):
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="issue_body_and_labels",
                        failure_code="issue_readback_mismatch",
                    )
        completed_steps.append("issue_body_and_labels")

        try:
            current_project = self._pbi_refinement_project_item(request, target, issue)
            current_issue = self._pbi_refinement_issue_state(request)
        except Exception:
            return result(
                "partial",
                issue,
                project_item,
                pending_step="project_status",
                failure_code="project_prewrite_readback_failed",
            )
        if (
            current_project.get("item_id") != project_item.get("item_id")
            or not isinstance(current_project.get("status"), str)
            or cast(str, current_project.get("status")).casefold()
            not in allowed_statuses
            or not self._pbi_refinement_issue_matches(
                current_issue, desired_body, expected_label_names, initial_sub_issues
            )
        ):
            return result(
                "partial",
                current_issue,
                current_project,
                pending_step="project_status",
                failure_code="project_prewrite_state_changed",
            )
        issue = current_issue
        project_item = current_project

        if project_item["status"] != desired_status:
            target_option_id = (
                target.backlog_option_id
                if desired_status == target.backlog_status
                else target.todo_option_id
            )
            try:
                self._client.execute(
                    UPDATE_PROJECT_STATUS_MUTATION,
                    {
                        "input": {
                            "projectId": target.project_node_id,
                            "itemId": project_item["item_id"],
                            "fieldId": target.status_field_id,
                            "value": {"singleSelectOptionId": target_option_id},
                        }
                    },
                )
            except Exception:
                try:
                    current_project = self._pbi_refinement_project_item(
                        request, target, issue
                    )
                except Exception:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="project_status",
                        failure_code="project_status_unconfirmed",
                    )
                if current_project.get("status") != desired_status:
                    return result(
                        "partial",
                        issue,
                        current_project,
                        pending_step="project_status",
                        failure_code="project_status_unconfirmed",
                    )
                project_item = current_project
            else:
                try:
                    project_item = self._pbi_refinement_project_item(
                        request, target, issue
                    )
                except Exception:
                    return result(
                        "partial",
                        issue,
                        current_project,
                        pending_step="project_status",
                        failure_code="project_status_readback_failed",
                    )
                if project_item.get("status") != desired_status:
                    return result(
                        "partial",
                        issue,
                        project_item,
                        pending_step="project_status",
                        failure_code="project_status_readback_mismatch",
                    )
        completed_steps.append("project_status")

        try:
            issue = self._pbi_refinement_issue_state(request)
            project_item = self._pbi_refinement_project_item(request, target, issue)
        except Exception:
            return result(
                "partial",
                issue,
                project_item,
                pending_step="final_readback",
                failure_code="final_readback_failed",
            )
        if (
            issue.get("state") != "OPEN"
            or not self._pbi_refinement_issue_matches(
                issue, desired_body, expected_label_names, initial_sub_issues
            )
            or project_item.get("item_id") != current_project.get("item_id")
            or project_item.get("status") != desired_status
        ):
            return result(
                "partial",
                issue,
                project_item,
                pending_step="final_readback",
                failure_code="final_readback_mismatch",
            )
        return result("complete", issue, project_item)

    @staticmethod
    def _resolve_pbi_refinement_label(
        labels_by_name: Mapping[str, list[Mapping[str, Any]]],
        name: str,
        *,
        require_description: bool,
    ) -> tuple[str, str]:
        matches = labels_by_name.get(name.casefold(), [])
        if len(matches) != 1:
            raise PbiRefinementMutationError(
                "Requested or attached label is missing or ambiguous",
                code="label_unavailable",
                status_code=422,
            )
        label_id = matches[0].get("id")
        canonical_name = matches[0].get("name")
        description = matches[0].get("description")
        if (
            not isinstance(label_id, str)
            or not label_id
            or not isinstance(canonical_name, str)
            or (
                require_description
                and (not isinstance(description, str) or not description.strip())
            )
        ):
            raise PbiRefinementMutationError(
                "Requested label or its live description is incomplete",
                code="label_metadata_unavailable",
                status_code=422,
            )
        return label_id, canonical_name

    @staticmethod
    def _pbi_refinement_label_names(issue: Mapping[str, Any]) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for label in _nodes(issue.get("labels")):
            name = label.get("name")
            if not isinstance(name, str) or not name:
                raise PbiRefinementMutationError(
                    "GitHub returned an invalid issue label",
                    code="issue_labels_invalid",
                    status_code=409,
                )
            if name.casefold() in seen:
                raise PbiRefinementMutationError(
                    "GitHub returned duplicate issue labels",
                    code="issue_labels_ambiguous",
                    status_code=409,
                )
            seen.add(name.casefold())
            names.append(name)
        return tuple(names)

    @staticmethod
    def _pbi_refinement_sub_issues(
        issue: Mapping[str, Any],
    ) -> tuple[Mapping[str, object], ...]:
        sub_issues: list[Mapping[str, object]] = []
        for item in _nodes(issue.get("subIssues")):
            number = item.get("number")
            title = item.get("title")
            state = item.get("state")
            if (
                type(number) is not int
                or number <= 0
                or not isinstance(title, str)
                or not isinstance(state, str)
            ):
                raise PbiRefinementMutationError(
                    "GitHub returned invalid linked sub-issue data",
                    code="sub_issues_invalid",
                    status_code=409,
                )
            sub_issues.append({"number": number, "title": title, "state": state})
        return tuple(sorted(sub_issues, key=lambda item: cast(int, item["number"])))

    @classmethod
    def _pbi_refinement_issue_matches(
        cls,
        issue: Mapping[str, Any],
        expected_body: str,
        expected_labels: set[str],
        expected_sub_issues: tuple[Mapping[str, object], ...],
    ) -> bool:
        body = issue.get("body")
        if body is None:
            body = ""
        try:
            return (
                issue.get("state") == "OPEN"
                and body == expected_body
                and {name.casefold() for name in cls._pbi_refinement_label_names(issue)}
                == expected_labels
                and cls._pbi_refinement_sub_issues(issue) == expected_sub_issues
            )
        except PbiRefinementMutationError:
            return False

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        """Create once, reconcile known issue state, and verify the Project item."""

        owner, name = self._repository_parts(request.repository)
        current = progress

        def save_progress(
            step: str,
            *,
            completed: bool = False,
            **changes: object,
        ) -> None:
            nonlocal current
            completed_steps = list(current.completed_steps)
            if completed and step not in completed_steps:
                completed_steps.append(step)
            current = replace(
                current,
                current_step=step,
                completed_steps=tuple(completed_steps),
                **changes,
            )
            checkpoint(current)

        if current.issue_id is None:
            if current.issue_create_started:
                raise PbiCreationError(
                    "Issue creation outcome is unknown",
                    code="outcome_unknown",
                    status_code=202,
                    unknown_outcome=True,
                )
            save_progress("create_issue", issue_create_started=True)
            status, created = self._rest_request(
                "POST",
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues",
                {
                    "title": request.title,
                    "body": request.body,
                    "labels": list(request.labels),
                },
            )
            if status != 201:
                save_progress("create_issue", issue_create_started=False)
                raise PbiCreationError(
                    "GitHub rejected issue creation",
                    code="issue_create_rejected",
                    status_code=502,
                )
            issue_id = created.get("node_id")
            issue_number = created.get("number")
            issue_url = created.get("html_url") or created.get("url")
            if (
                not isinstance(issue_id, str)
                or not issue_id
                or type(issue_number) is not int
                or issue_number <= 0
                or not isinstance(issue_url, str)
                or not issue_url
            ):
                raise PbiCreationError(
                    "GitHub did not return a durable issue identity",
                    code="outcome_unknown",
                    status_code=202,
                    unknown_outcome=True,
                )
            save_progress(
                "issue_created",
                completed=True,
                issue_create_started=False,
                issue_id=issue_id,
                issue_number=issue_number,
                issue_url=issue_url,
            )

        if current.issue_number is None or current.issue_id is None:
            raise PbiCreationError(
                "Saved issue identity is incomplete",
                code="issue_identity_incomplete",
                status_code=502,
            )

        issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
        self._validate_pbi_creation_issue(request, current, issue)
        labels = _nodes(issue.get("labels"))
        present_label_names = {
            label.get("name") for label in labels if isinstance(label.get("name"), str)
        }
        missing_labels = [
            (label, label_id)
            for label, label_id in zip(request.labels, target.label_ids, strict=True)
            if label not in present_label_names
        ]
        if missing_labels:
            save_progress("apply_labels")
            self._client.execute(
                ADD_ISSUE_LABELS_MUTATION,
                {
                    "input": {
                        "labelableId": current.issue_id,
                        "labelIds": [label_id for _, label_id in missing_labels],
                    }
                },
            )
            issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
            self._validate_pbi_creation_issue(request, current, issue)
            labels = _nodes(issue.get("labels"))
            present_label_names = {
                label.get("name")
                for label in labels
                if isinstance(label.get("name"), str)
            }
        if not set(request.labels).issubset(present_label_names):
            raise PbiCreationError(
                "Requested labels did not read back",
                code="labels_unconfirmed",
                status_code=502,
            )
        save_progress("labels_applied", completed=True)

        project_item = self._pbi_creation_project_item(
            request, target, current.issue_id, current.issue_number
        )
        if project_item is None:
            save_progress("add_to_project")
            self._client.execute(
                ADD_PROJECT_ITEM_MUTATION,
                {
                    "input": {
                        "projectId": target.project_node_id,
                        "contentId": current.issue_id,
                    }
                },
            )
            project_item = self._pbi_creation_project_item(
                request, target, current.issue_id, current.issue_number
            )
        if project_item is None:
            raise PbiCreationError(
                "Project membership did not read back",
                code="project_membership_unconfirmed",
                status_code=502,
            )
        item_id = project_item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise PbiCreationError(
                "Project item identity is incomplete",
                code="project_item_identity_incomplete",
                status_code=502,
            )
        if current.project_item_id is not None and current.project_item_id != item_id:
            raise PbiCreationError(
                "Project item identity changed during reconciliation",
                code="project_item_identity_conflict",
                status_code=409,
            )
        save_progress("project_added", completed=True, project_item_id=item_id)

        if project_item.get("status") != target.backlog_status:
            save_progress("set_backlog")
            self._client.execute(
                UPDATE_PROJECT_STATUS_MUTATION,
                {
                    "input": {
                        "projectId": target.project_node_id,
                        "itemId": item_id,
                        "fieldId": target.status_field_id,
                        "value": {"singleSelectOptionId": target.backlog_option_id},
                    }
                },
            )
            project_item = self._pbi_creation_project_item(
                request, target, current.issue_id, current.issue_number
            )
        if project_item is None or project_item.get("status") != target.backlog_status:
            raise PbiCreationError(
                "Project Backlog status did not read back",
                code="project_status_unconfirmed",
                status_code=502,
            )
        save_progress("status_backlog", completed=True)

        issue = self._pbi_creation_issue_state(owner, name, current.issue_number)
        self._validate_pbi_creation_issue(request, current, issue)
        actual_labels = tuple(
            sorted(
                {
                    str(label["name"])
                    for label in _nodes(issue.get("labels"))
                    if isinstance(label.get("name"), str)
                }
            )
        )
        project_item = self._pbi_creation_project_item(
            request, target, current.issue_id, current.issue_number
        )
        if (
            project_item is None
            or project_item.get("item_id") != item_id
            or project_item.get("status") != target.backlog_status
        ):
            raise PbiCreationError(
                "Final Project state did not read back",
                code="project_state_unconfirmed",
                status_code=502,
            )
        return PbiCreationResult(
            issue_id=current.issue_id,
            issue_number=current.issue_number,
            issue_url=current.issue_url or str(issue.get("url", "")),
            labels=actual_labels,
            project_item_id=item_id,
            project_status=target.backlog_status,
            completed_steps=current.completed_steps,
        )

    def _pbi_creation_issue_state(
        self, owner: str, name: str, issue_number: int
    ) -> Mapping[str, Any]:
        data = self._client.execute(
            PBI_CREATION_ISSUE_QUERY,
            {"owner": owner, "name": name, "number": issue_number},
        )
        issue = _mapping(_mapping(data.get("repository")).get("issue"))
        if not issue:
            raise PbiCreationError(
                "Created issue could not be read back",
                code="issue_readback_failed",
                status_code=502,
            )
        return issue

    @staticmethod
    def _validate_pbi_creation_issue(
        request: PbiCreationRequest,
        progress: PbiCreationProgress,
        issue: Mapping[str, Any],
    ) -> None:
        if (
            issue.get("id") != progress.issue_id
            or issue.get("number") != progress.issue_number
            or issue.get("title") != request.title
            or issue.get("body") != request.body
            or issue.get("state") != "OPEN"
        ):
            raise PbiCreationError(
                "Issue identity or caller content did not read back",
                code="issue_identity_or_content_conflict",
                status_code=409,
            )

    def _pbi_creation_project_item(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        issue_id: str,
        issue_number: int,
    ) -> dict[str, object] | None:
        cursor: str | None = None
        matching_items: list[dict[str, object]] = []
        project_id: str | None = None
        status_field_id: str | None = None
        backlog_option_id: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(PBI_CREATION_PROJECT_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "cursor": cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if current_project_id != target.project_node_id:
                raise PbiCreationScopeError("Configured Project identity changed")
            if project_id is not None and current_project_id != project_id:
                raise ProviderError("GitHub returned conflicting Project identities")
            project_id = (
                current_project_id if isinstance(current_project_id, str) else None
            )

            fields = [
                field
                for field in _nodes(project.get("fields"))
                if isinstance(field.get("name"), str)
                and str(field.get("name")).casefold() == "status"
            ]
            if len(fields) != 1:
                raise PbiCreationValidationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                )
            field_id = fields[0].get("id")
            options = fields[0].get("options")
            if not isinstance(field_id, str) or field_id != target.status_field_id:
                raise PbiCreationValidationError(
                    "Configured Project Status field changed",
                    code="project_status_unavailable",
                )
            if not isinstance(options, list):
                raise PbiCreationValidationError(
                    "Configured Project Status options are incomplete",
                    code="project_status_unavailable",
                )
            backlog_options: list[Mapping[str, object]] = []
            for raw_option in cast(list[object], options):
                if not isinstance(raw_option, Mapping):
                    continue
                option = cast(Mapping[str, object], raw_option)
                option_name = option.get("name")
                if isinstance(option_name, str) and option_name.casefold() == "backlog":
                    backlog_options.append(option)
            if len(backlog_options) != 1:
                raise PbiCreationValidationError(
                    "Configured Project Backlog option changed",
                    code="project_backlog_unavailable",
                )
            option_id = backlog_options[0].get("id")
            if not isinstance(option_id, str) or option_id != target.backlog_option_id:
                raise PbiCreationValidationError(
                    "Configured Project Backlog option changed",
                    code="project_backlog_unavailable",
                )
            status_field_id = field_id
            backlog_option_id = option_id

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                raw_content: object = raw_item.get("content")
                if not isinstance(raw_content, Mapping):
                    continue
                content = cast(Mapping[str, Any], raw_content)
                if content.get("__typename") != "Issue":
                    continue
                repository_value: object = content.get("repository")
                repository_name = (
                    cast(Mapping[str, Any], repository_value).get("nameWithOwner")
                    if isinstance(repository_value, Mapping)
                    else None
                )
                matches = content.get("id") == issue_id or (
                    content.get("number") == issue_number
                    and isinstance(repository_name, str)
                    and repository_name.casefold() == request.repository.casefold()
                )
                if not matches:
                    continue
                status_values: list[Mapping[str, Any]] = []
                for value in _nodes(raw_item.get("fieldValues")):
                    raw_field: object = value.get("field")
                    if not isinstance(raw_field, Mapping):
                        continue
                    field = cast(Mapping[str, Any], raw_field)
                    if field.get("id") == field_id:
                        status_values.append(value)
                if len(status_values) > 1:
                    raise ProviderError("Project item has conflicting Status values")
                raw_status = status_values[0].get("name") if status_values else None
                status = raw_status if isinstance(raw_status, str) else None
                matching_items.append(
                    {
                        "item_id": raw_item.get("id"),
                        "status": status,
                    }
                )
            has_next, cursor = _next_cursor(items)
            if not has_next:
                break

        if (
            status_field_id != target.status_field_id
            or backlog_option_id != target.backlog_option_id
        ):
            raise ProviderError("Project creation configuration changed")
        if len(matching_items) > 1:
            raise PbiCreationError(
                "Issue appears more than once in the configured Project",
                code="duplicate_project_items",
                status_code=409,
            )
        return matching_items[0] if matching_items else None

    def prepare_pbi_relations(self, request: PbiRelationRequest) -> PbiRelationSnapshot:
        request.validate()
        if request.project_id != self.project_id:
            raise PbiRelationScopeError()
        owner, name = self._repository_parts(request.repository)
        repository = f"{owner}/{name}"
        if repository.casefold() != request.repository.casefold():
            raise PbiRelationValidationError(
                "Repository identity is not canonical", code="invalid_repository"
            )

        linked_repositories, project_items = self._pbi_relation_project_state()
        if repository.casefold() not in linked_repositories:
            raise PbiRelationScopeError(
                "Repository is not linked to the configured Project"
            )

        parent, parent_payload = self._pbi_relation_issue_state(
            repository, request.parent_issue_number
        )
        if parent.state != "OPEN":
            raise PbiRelationValidationError(
                "Parent PBI must be open", code="parent_not_open", status_code=409
            )
        body = parent_payload.get("body")
        if not isinstance(body, str):
            raise PbiRelationProviderError("parent_body_unavailable")
        if not has_refined_pbi_sections(body):
            raise PbiRelationValidationError(
                "Parent PBI does not have the required refined sections",
                code="parent_not_refined",
                status_code=409,
            )

        parent_item = self._pbi_relation_project_item(
            project_items, parent.node_id, repository
        )
        if str(parent_item.get("status", "")).casefold() not in {
            "todo",
            "in progress",
        }:
            raise PbiRelationValidationError(
                "Parent PBI must be in Todo or In Progress",
                code="parent_not_refined",
                status_code=409,
            )

        children: list[PbiRelationIssue] = []
        for reference in request.children:
            issue, _ = self._pbi_relation_issue_state(repository, reference.number)
            if (
                issue.node_id != reference.node_id
                or issue.number != reference.number
                or issue.url != reference.url
            ):
                raise PbiRelationValidationError(
                    "Created child reference does not match the live issue",
                    code="child_identity_conflict",
                    status_code=409,
                )
            if issue.state != "OPEN":
                raise PbiRelationValidationError(
                    "Created child must remain open",
                    code="child_not_open",
                    status_code=409,
                )
            self._pbi_relation_project_item(
                project_items,
                issue.node_id,
                repository,
                expected_item_id=reference.project_item_id,
            )
            children.append(issue)

        child_numbers = {issue.number for issue in children}
        parent_by_child: dict[int, int | None] = {}
        for child in children:
            existing_parent = self._pbi_relation_parent_number(repository, child.number)
            if existing_parent not in (None, parent.number):
                raise PbiRelationValidationError(
                    "Child already belongs to another parent",
                    code="child_has_other_parent",
                    status_code=409,
                )
            parent_by_child[child.number] = existing_parent
        self._pbi_relation_check_parent_chain(repository, parent.number, child_numbers)
        parent_sub_issues = self.list_pbi_sub_issues(repository, parent.number)
        return PbiRelationSnapshot(
            parent,
            tuple(children),
            parent_sub_issues,
            parent_by_child,
        )

    def _pbi_relation_project_state(
        self,
    ) -> tuple[set[str], dict[str, list[dict[str, object]]]]:
        repository_cursor: str | None = None
        item_cursor: str | None = None
        seen_repository_cursors: set[str] = set()
        seen_item_cursors: set[str] = set()
        project_id: str | None = None
        status_field_id: str | None = None
        linked_repositories: set[str] = set()
        project_items: dict[str, list[dict[str, object]]] = {}
        while True:
            data = self._client.execute(
                _owner_query(PBI_RELATION_PROJECT_QUERY, self.owner_type),
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "repositoryCursor": repository_cursor,
                    "itemCursor": item_cursor,
                },
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                raise PbiRelationProviderError("project_unavailable")
            if project_id is not None and project_id != current_project_id:
                raise PbiRelationProviderError("project_identity_changed")
            project_id = current_project_id

            fields_value = project.get("fields")
            if not isinstance(fields_value, Mapping):
                raise PbiRelationProviderError("project_fields_incomplete")
            fields = cast(Mapping[str, Any], fields_value)
            field_nodes = fields.get("nodes")
            if not isinstance(field_nodes, list):
                raise PbiRelationProviderError("project_fields_incomplete")
            status_fields: list[Mapping[str, Any]] = []
            for raw_field in cast(list[object], field_nodes):
                if not isinstance(raw_field, Mapping):
                    continue
                field = cast(Mapping[str, Any], raw_field)
                field_name = field.get("name")
                if isinstance(field_name, str) and field_name.casefold() == "status":
                    status_fields.append(field)
            if len(status_fields) != 1:
                raise PbiRelationProviderError("project_status_unavailable")
            raw_status_field_id = status_fields[0].get("id")
            if not isinstance(raw_status_field_id, str) or not raw_status_field_id:
                raise PbiRelationProviderError("project_status_unavailable")
            if status_field_id is not None and status_field_id != raw_status_field_id:
                raise PbiRelationProviderError("project_status_changed")
            status_field_id = raw_status_field_id

            repository_nodes, has_repository_page, next_repository_cursor = (
                self._pbi_relation_connection(project.get("repositories"))
            )
            for raw_repository in repository_nodes:
                repository_name = _mapping(raw_repository).get("nameWithOwner")
                if not isinstance(repository_name, str) or not repository_name:
                    raise PbiRelationProviderError("project_repository_incomplete")
                linked_repositories.add(repository_name.casefold())

            item_nodes, has_item_page, next_item_cursor = self._pbi_relation_connection(
                project.get("items")
            )
            for raw_item in item_nodes:
                item = _mapping(raw_item)
                raw_content = item.get("content")
                if raw_content is None:
                    continue
                content = _mapping(raw_content)
                if content.get("__typename") != "Issue":
                    continue
                node_id = content.get("id")
                number = content.get("number")
                content_repository = _mapping(content.get("repository")).get(
                    "nameWithOwner"
                )
                item_id = item.get("id")
                if (
                    not isinstance(node_id, str)
                    or type(number) is not int
                    or not isinstance(content_repository, str)
                    or not isinstance(item_id, str)
                ):
                    raise PbiRelationProviderError("project_item_incomplete")
                field_values = item.get("fieldValues")
                values, has_more_values, _ = self._pbi_relation_connection(field_values)
                if has_more_values:
                    raise PbiRelationProviderError("project_item_fields_incomplete")
                statuses: list[str] = []
                for raw_value in values:
                    value = _mapping(raw_value)
                    raw_field = value.get("field")
                    if raw_field is None:
                        continue
                    field = _mapping(raw_field)
                    if field.get("id") == status_field_id:
                        name_value = value.get("name")
                        if not isinstance(name_value, str) or not name_value:
                            raise PbiRelationProviderError(
                                "project_item_status_incomplete"
                            )
                        statuses.append(name_value)
                if len(statuses) > 1:
                    raise PbiRelationProviderError("project_item_status_ambiguous")
                project_items.setdefault(node_id, []).append(
                    {
                        "id": item_id,
                        "number": number,
                        "repository": content_repository,
                        "status": statuses[0] if statuses else None,
                    }
                )

            repository_cursor = next_repository_cursor
            item_cursor = next_item_cursor
            if has_repository_page and (
                not repository_cursor or repository_cursor in seen_repository_cursors
            ):
                raise PbiRelationProviderError(
                    "project_repository_pagination_incomplete"
                )
            if has_item_page and (not item_cursor or item_cursor in seen_item_cursors):
                raise PbiRelationProviderError("project_item_pagination_incomplete")
            if repository_cursor:
                seen_repository_cursors.add(repository_cursor)
            if item_cursor:
                seen_item_cursors.add(item_cursor)
            if not has_repository_page and not has_item_page:
                return linked_repositories, project_items

    @staticmethod
    def _pbi_relation_connection(
        connection_value: object,
    ) -> tuple[list[object], bool, str | None]:
        connection = _mapping(connection_value)
        nodes = connection.get("nodes")
        page_info = _mapping(connection.get("pageInfo"))
        has_next = page_info.get("hasNextPage")
        cursor = page_info.get("endCursor")
        if not isinstance(nodes, list) or type(has_next) is not bool:
            raise PbiRelationProviderError("github_connection_incomplete")
        if has_next and (not isinstance(cursor, str) or not cursor):
            raise PbiRelationProviderError("github_cursor_missing")
        return (
            cast(list[object], nodes),
            has_next,
            cursor if isinstance(cursor, str) else None,
        )

    @staticmethod
    def _pbi_relation_project_item(
        project_items: Mapping[str, list[dict[str, object]]],
        node_id: str,
        repository: str,
        *,
        expected_item_id: str | None = None,
    ) -> dict[str, object]:
        matches = project_items.get(node_id, [])
        if len(matches) != 1:
            raise PbiRelationValidationError(
                "Issue must appear exactly once in the configured Project",
                code="project_item_unavailable",
                status_code=409,
            )
        item = matches[0]
        if str(item.get("repository", "")).casefold() != repository.casefold():
            raise PbiRelationValidationError(
                "Issue is in another repository", code="cross_repository_issue"
            )
        if expected_item_id is not None and item.get("id") != expected_item_id:
            raise PbiRelationValidationError(
                "Created child Project item does not match the live item",
                code="project_item_conflict",
                status_code=409,
            )
        return item

    def _pbi_relation_issue_state(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, Mapping[str, Any]]:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}"
        )
        status, payload = self._rest_request("GET", path)
        if status != 200:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))
        issue = self._pbi_relation_issue(payload)
        if issue.number != issue_number:
            raise PbiRelationProviderError("issue_identity_conflict")
        return issue, payload

    @staticmethod
    def _pbi_relation_issue(payload: Mapping[str, Any]) -> PbiRelationIssue:
        issue_id = payload.get("id")
        node_id = payload.get("node_id")
        number = payload.get("number")
        url = payload.get("html_url") or payload.get("url")
        title = payload.get("title")
        state = payload.get("state")
        state_reason = payload.get("state_reason")
        if (
            type(issue_id) is not int
            or issue_id <= 0
            or not isinstance(node_id, str)
            or not node_id
            or type(number) is not int
            or number <= 0
            or not isinstance(url, str)
            or not url
            or not isinstance(title, str)
            or not isinstance(state, str)
            or (state_reason is not None and not isinstance(state_reason, str))
        ):
            raise PbiRelationProviderError("issue_response_incomplete")
        return PbiRelationIssue(
            issue_id,
            node_id,
            number,
            url,
            title,
            state.upper(),
            state_reason,
        )

    def _pbi_relation_parent_number(
        self, repository: str, issue_number: int
    ) -> int | None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}/parent"
        )
        status, payload = self._rest_request("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))
        return self._pbi_relation_issue(payload).number

    def _pbi_relation_check_parent_chain(
        self, repository: str, parent_issue_number: int, child_numbers: set[int]
    ) -> None:
        current = parent_issue_number
        visited = {current}
        for _ in range(100):
            ancestor = self._pbi_relation_parent_number(repository, current)
            if ancestor is None:
                return
            if ancestor in child_numbers:
                raise PbiRelationValidationError(
                    "Parent-child declarations would create a cycle",
                    code="sub_issue_cycle",
                    status_code=409,
                )
            if ancestor in visited:
                raise PbiRelationProviderError("sub_issue_graph_inconsistent")
            visited.add(ancestor)
            current = ancestor
        raise PbiRelationProviderError("sub_issue_graph_too_deep")

    @staticmethod
    def _pbi_relation_status_code(status: int) -> str:
        if status in {401, 403}:
            return "permission_denied"
        if status in {404, 410}:
            return "issue_unavailable"
        return "github_request_failed"

    def _pbi_relation_list_issues(
        self, repository: str, issue_number: int, relation: str
    ) -> tuple[PbiRelationIssue, ...]:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue_number}/"
        )
        path += {
            "sub_issues": "sub_issues",
            "blocked_by": "dependencies/blocked_by",
            "blocking": "dependencies/blocking",
        }[relation]
        results: list[PbiRelationIssue] = []
        page = 1
        while True:
            status, payload = self._rest_list_request(
                "GET", f"{path}?per_page=100&page={page}"
            )
            if status != 200:
                raise PbiRelationProviderError(self._pbi_relation_status_code(status))
            if (
                len(payload) > 100
                or len(results) + len(payload) > MAX_PBI_RELATION_GRAPH_ISSUES
            ):
                raise PbiRelationProviderError("relation_set_too_large")
            for raw_issue in payload:
                if not isinstance(raw_issue, Mapping):
                    raise PbiRelationProviderError("relation_response_incomplete")
                results.append(
                    self._pbi_relation_issue(cast(Mapping[str, Any], raw_issue))
                )
            if len(payload) < 100:
                return tuple(results)
            page += 1

    def _rest_list_request(self, method: str, path: str) -> tuple[int, list[object]]:
        request_rest = getattr(self._client, "request_rest", None)
        if not callable(request_rest):
            raise PbiRelationProviderError("rest_api_unavailable")
        rest_call = cast(
            Callable[
                [str, str, Mapping[str, object] | None],
                tuple[int, Mapping[str, Any] | list[Any]],
            ],
            request_rest,
        )
        status, payload = rest_call(method, path, None)
        if not isinstance(payload, list):
            raise PbiRelationProviderError("github_list_response_invalid")
        return status, cast(list[object], payload)

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(
            repository, parent_issue_number, "sub_issues"
        )

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None:
        return self._pbi_relation_parent_number(repository, child_issue_number)

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(repository, issue_number, "blocked_by")

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        return self._pbi_relation_list_issues(repository, issue_number, "blocking")

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{parent_issue_number}/sub_issues"
        )
        status, _ = self._rest_request("POST", path, {"sub_issue_id": child_issue_id})
        if status != 201:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        owner, name = self._repository_parts(repository)
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{blocked_issue_number}/dependencies/blocked_by"
        )
        status, _ = self._rest_request("POST", path, {"issue_id": blocker_issue_id})
        if status != 201:
            raise PbiRelationProviderError(self._pbi_relation_status_code(status))

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

    @staticmethod
    def _audit_action_parts(
        action: Mapping[str, object] | None,
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        if action is None:
            return {}, {}
        empty: Mapping[str, object] = {}
        raw_request = action.get("request")
        request_record = (
            cast(Mapping[str, object], raw_request)
            if isinstance(raw_request, Mapping)
            else empty
        )
        raw_target = request_record.get("target")
        raw_result = action.get("result")
        target = (
            cast(Mapping[str, object], raw_target)
            if isinstance(raw_target, Mapping)
            else empty
        )
        result = (
            cast(Mapping[str, object], raw_result)
            if isinstance(raw_result, Mapping)
            else empty
        )
        return target, result

    def _execute_completion_mutation(
        self, query: str, variables: Mapping[str, object]
    ) -> None:
        self._client.execute(query, variables)

    @staticmethod
    def _merge_details(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        details = payload.get("details")
        return (
            cast(Mapping[str, Any], details)
            if isinstance(details, Mapping)
            else payload
        )

    @staticmethod
    def _confirmed_merge_result(
        request: HandoffRequest,
        pull_request_number: int,
        expected_head: str,
        snapshot: PullRequestSnapshot,
    ) -> dict[str, object]:
        return {
            "status": "merged",
            "pull_request_id": f"{request.repository}#{pull_request_number}",
            "merge_action": "default",
            "head_sha": expected_head,
            "merge_commit_sha": snapshot.merge_commit_oid,
        }

    @staticmethod
    def _merge_snapshot_matches_handoff(
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        snapshot: PullRequestSnapshot,
    ) -> bool:
        return (
            snapshot.repository == request.repository
            and snapshot.number == pull_request_number
            and snapshot.url == pull_request_url
            and snapshot.merged
            and snapshot.source_repository == request.repository
            and snapshot.source_branch == request.branch
            and snapshot.target_branch == request.base_branch
            and snapshot.source_head in {None, expected_head}
            and snapshot.merge_commit_oid is not None
        )

    @staticmethod
    def _merge_target_matches_handoff(
        target: Mapping[str, object],
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
    ) -> bool:
        return (
            target.get("pull_request_number") == str(pull_request_number)
            and target.get("pull_request_url") == pull_request_url
            and target.get("head_sha") == expected_head
            and target.get("branch") == request.branch
            and target.get("base_branch") == request.base_branch
        )

    @staticmethod
    def _retry_delay(error: Exception) -> float | None:
        if isinstance(error, GitHubRateLimitError):
            if error.retry_after is not None and error.retry_after >= 0:
                return error.retry_after
            if error.reset_at is not None:
                return max(0.0, error.reset_at - time.time())
        if isinstance(error, GitHubOutcomeUnknownError) and error.status_code in {
            500,
            502,
            503,
            504,
        }:
            return 0.0
        return None

    @staticmethod
    def _provider_failure_status(error: ProviderError) -> str:
        return (
            "deferred"
            if isinstance(error, (GitHubRateLimitError, GitHubOutcomeUnknownError))
            else "operator_required"
        )

    def _handoff_action(
        self, request: HandoffRequest, mutation: str, operation_key: str
    ) -> dict[str, object] | None:
        if request.mutation_audit is None:
            return None
        return request.mutation_audit.handoff_mutation_action(
            request, mutation, operation_key
        )

    def _rest_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        request_rest = getattr(self._client, "request_rest", None)
        if not callable(request_rest):
            raise ProviderError("GitHub REST mutations are not configured")
        return cast(tuple[int, Mapping[str, Any]], request_rest(method, path, payload))

    def _issue_completion_state(self, request: HandoffRequest) -> Mapping[str, object]:
        owner, name = self._repository_parts(request.repository)
        data = self._client.execute(
            ISSUE_COMPLETION_QUERY,
            {"owner": owner, "name": name, "number": request.pbi_number},
        )
        raw_issue = _mapping(data.get("repository")).get("issue")
        if raw_issue is None:
            return {"valid": False, "error": "The linked issue no longer exists"}
        issue = _mapping(raw_issue)
        issue_id = issue.get("id")
        number = issue.get("number")
        state = issue.get("state")
        valid = (
            isinstance(issue_id, str)
            and bool(issue_id)
            and type(number) is int
            and number == request.pbi_number
            and state in {"OPEN", "CLOSED"}
        )
        return {
            "valid": valid,
            "error": "GitHub returned conflicting issue identity or state",
            "issue_id": issue_id if isinstance(issue_id, str) else "",
            "state": state if isinstance(state, str) else "",
        }

    def _source_ref_completion_state(
        self, request: HandoffRequest, expected_head: str
    ) -> Mapping[str, object]:
        owner, name = self._repository_parts(request.repository)
        qualified_name = f"refs/heads/{request.branch}"
        data = self._client.execute(
            BRANCH_REF_QUERY,
            {"owner": owner, "name": name, "qualifiedName": qualified_name},
        )
        repository = _mapping(data.get("repository"))
        repository_id = repository.get("id")
        if not isinstance(repository_id, str) or not repository_id:
            return {
                "valid": False,
                "error": "GitHub omitted source repository identity",
            }
        if "ref" not in repository:
            return {"valid": False, "error": "GitHub omitted the source ref"}
        raw_ref = repository.get("ref")
        if raw_ref is None:
            return {"valid": True, "exists": False, "repository_id": repository_id}
        ref = _mapping(raw_ref)
        ref_id = ref.get("id")
        ref_name = ref.get("name")
        oid = _commit_oid(_mapping(ref.get("target")).get("oid"))
        if (
            not isinstance(ref_id, str)
            or not ref_id
            or ref_name != qualified_name
            or oid is None
        ):
            return {
                "valid": False,
                "error": "GitHub returned conflicting source ref identity",
            }
        return {
            "valid": True,
            "exists": True,
            "repository_id": repository_id,
            "ref_id": ref_id,
            "ref_name": qualified_name,
            "head_sha": oid,
            "expected_head": expected_head,
        }

    def _project_completion_state(
        self, request: HandoffRequest
    ) -> Mapping[str, object]:
        if request.project_id != self.project_id:
            return {"valid": False, "error": "The handoff belongs to another Project"}
        cursor: str | None = None
        matching_items: list[Mapping[str, object]] = []
        status_field: Mapping[str, object] | None = None
        done_option_id: str | None = None
        project_id: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(COMPLETION_PROJECT_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                return {"valid": False, "error": "GitHub omitted Project identity"}
            if project_id is not None and current_project_id != project_id:
                return {
                    "valid": False,
                    "error": "GitHub returned conflicting Project identity",
                }
            project_id = current_project_id

            fields = [
                field
                for field in _nodes(project.get("fields", {}))
                if field.get("name") == "Status"
            ]
            if len(fields) != 1:
                return {"valid": False, "error": "Project must have one Status field"}
            status_field = fields[0]
            field_id = status_field.get("id")
            options = status_field.get("options")
            if not isinstance(field_id, str) or not isinstance(options, list):
                return {"valid": False, "error": "Project Status field is incomplete"}
            done_option_ids: list[str] = []
            for option in cast(list[object], options):
                if not isinstance(option, Mapping):
                    continue
                option_record = cast(Mapping[str, object], option)
                option_id = option_record.get("id")
                if option_record.get("name") == "Done" and isinstance(option_id, str):
                    done_option_ids.append(option_id)
            if len(done_option_ids) != 1:
                return {
                    "valid": False,
                    "error": "Project Status has no unique Done option",
                }
            done_option_id = done_option_ids[0]

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                content = _mapping(raw_item.get("content"))
                repository = _mapping(content.get("repository"))
                if (
                    content.get("__typename") == "Issue"
                    and type(content.get("number")) is int
                    and content.get("number") == request.pbi_number
                    and repository.get("nameWithOwner") == request.repository
                ):
                    values = [
                        value
                        for value in _nodes(raw_item.get("fieldValues", {}))
                        if _mapping(value.get("field")).get("id") == field_id
                    ]
                    if len(values) != 1:
                        matching_items.append({"valid": False})
                    else:
                        value = values[0]
                        matching_items.append(
                            {
                                "valid": True,
                                "item_id": raw_item.get("id"),
                                "status": value.get("name"),
                                "option_id": value.get("optionId"),
                            }
                        )
            has_next, cursor = _next_cursor(items)
            if not has_next:
                break

        if len(matching_items) != 1:
            return {"valid": False, "error": "Expected one exact Project issue item"}
        item = matching_items[0]
        item_id = item.get("item_id")
        field_id = status_field.get("id")
        if (
            item.get("valid") is not True
            or not isinstance(item_id, str)
            or not item_id
            or not isinstance(field_id, str)
        ):
            return {
                "valid": False,
                "error": "Project item Status evidence is incomplete",
            }
        return {
            "valid": True,
            "project_id": project_id or "",
            "item_id": item_id,
            "field_id": field_id,
            "status": item.get("status") if isinstance(item.get("status"), str) else "",
            "option_id": item.get("option_id")
            if isinstance(item.get("option_id"), str)
            else "",
            "done_option_id": done_option_id,
        }

    def _ensure_completion_step(
        self,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        target: Mapping[str, object],
        inspect_state: Callable[[], Mapping[str, object]],
        is_complete: Callable[[Mapping[str, object]], bool],
        can_apply: Callable[[Mapping[str, object]], bool],
        apply: Callable[[Mapping[str, object]], None],
    ) -> dict[str, object]:
        for _ in range(2):
            try:
                state = inspect_state()
            except ProviderError as exc:
                return {
                    "status": self._provider_failure_status(exc),
                    "error_class": type(exc).__name__,
                }
            if state.get("valid") is not True:
                return {
                    "status": "operator_required",
                    "reason": state.get("error", "Completion identity is unproven"),
                }

            action = self._handoff_action(request, mutation, operation_key)
            if is_complete(state):
                if action is None:
                    action_id = _begin_handoff_mutation(
                        request, mutation, operation_key, target
                    )
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                elif isinstance(action.get("id"), str):
                    _finish_handoff_mutation(
                        request,
                        cast(str, action["id"]),
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                return {"status": "completed", "reconciliation": "readback_present"}

            if not can_apply(state):
                return {
                    "status": "operator_required",
                    "reason": "Remote completion state changed unexpectedly",
                }

            if action is not None:
                action_target, action_result = self._audit_action_parts(action)
                action_status = action.get("status")
                request_record = action.get("request")
                attempt = (
                    cast(Mapping[str, object], request_record).get("attempt")
                    if isinstance(request_record, Mapping)
                    else None
                )
                if action_status in {"pending", "uncertain"}:
                    return {"status": "deferred", "reason": "prior_write_unresolved"}
                if action_status == "succeeded":
                    return {
                        "status": "operator_required",
                        "reason": "previous_write_readback_conflicts",
                    }
                if (
                    action_status != "failed"
                    or action_result.get("retryable") is not True
                ):
                    return {
                        "status": "operator_required",
                        "reason": "previous_write_not_retryable",
                    }
                retry_at = action_result.get("retry_after_at")
                if isinstance(retry_at, (int, float)) and time.time() < retry_at:
                    return {
                        "status": "deferred",
                        "retry_after_at": retry_at,
                    }
                if type(attempt) is int and attempt >= 2:
                    return {"status": "deferred", "reason": "retry_limit_reached"}
                if any(
                    action_target.get(key) != value for key, value in target.items()
                ):
                    return {
                        "status": "operator_required",
                        "reason": "prior_write_target_changed",
                    }

            action_id = _begin_handoff_mutation(
                request, mutation, operation_key, target
            )
            try:
                apply(state)
            except ProviderError as exc:
                try:
                    observed = inspect_state()
                except ProviderError as read_error:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "uncertain",
                        {
                            "reconciliation": "readback_unavailable",
                            "error_class": type(read_error).__name__,
                        },
                    )
                    return {
                        "status": self._provider_failure_status(read_error),
                        "error_class": type(read_error).__name__,
                    }
                if observed.get("valid") is True and is_complete(observed):
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                    return {"status": "completed", "reconciliation": "readback_present"}
                delay = self._retry_delay(exc)
                attempt = 1
                latest = self._handoff_action(request, mutation, operation_key)
                if latest is not None:
                    attempt_record = latest.get("request")
                    if isinstance(attempt_record, Mapping):
                        raw_attempt = cast(Mapping[str, object], attempt_record).get(
                            "attempt"
                        )
                        if type(raw_attempt) is int:
                            attempt = raw_attempt
                retry_at = time.time() + delay if delay is not None else None
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "failed",
                    {
                        "reconciliation": "readback_absent",
                        "error_class": type(exc).__name__,
                        "retryable": delay is not None,
                        **(
                            {"retry_after_at": retry_at} if retry_at is not None else {}
                        ),
                    },
                )
                if delay is not None and attempt == 1:
                    if delay > 1.0:
                        return {"status": "deferred", "retry_after_at": retry_at}
                    if delay > 0:
                        time.sleep(delay)
                    continue
                return {
                    "status": "deferred" if delay is not None else "operator_required",
                    "error_class": type(exc).__name__,
                    **({"reason": "retry_limit_reached"} if delay is not None else {}),
                }

            try:
                observed = inspect_state()
            except ProviderError as exc:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {
                        "reconciliation": "readback_unavailable",
                        "error_class": type(exc).__name__,
                    },
                )
                return {
                    "status": self._provider_failure_status(exc),
                    "error_class": type(exc).__name__,
                }
            if observed.get("valid") is True and is_complete(observed):
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "succeeded",
                    {"reconciliation": "readback_present"},
                )
                return {"status": "completed", "reconciliation": "readback_present"}
            _finish_handoff_mutation(
                request,
                action_id,
                "uncertain",
                {"reconciliation": "write_not_confirmed"},
            )
            return {"status": "deferred", "reason": "write_not_confirmed"}
        return {"status": "deferred", "reason": "retry_limit_reached"}

    def _merge_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> dict[str, object]:
        operation_key = f"{request.repository}#{pull_request_number}:{expected_head}"
        authorization_was_supplied = authorization is not None
        action = self._handoff_action(request, "merge_pull_request", operation_key)
        snapshot = self.get_pull_request(request.repository, pull_request_number)
        if (
            snapshot.repository != request.repository
            or snapshot.url != pull_request_url
        ):
            return {
                "status": "operator_required",
                "reason": "pull_request_identity_mismatch",
            }
        target, result = self._audit_action_parts(action)
        if snapshot.merged:
            if (
                action is None
                or action.get("status") not in {"pending", "uncertain", "succeeded"}
                or not self._merge_target_matches_handoff(
                    target,
                    request,
                    pull_request_number,
                    pull_request_url,
                    expected_head,
                )
                or not isinstance(target.get("review_cycle_id"), str)
                or not self._merge_snapshot_matches_handoff(
                    request,
                    pull_request_number,
                    pull_request_url,
                    expected_head,
                    snapshot,
                )
            ):
                return {
                    "status": "operator_required",
                    "reason": "merged_without_matching_authorization_audit",
                }
            audited_merge_sha = result.get("merge_commit_sha")
            if audited_merge_sha is not None and (
                not isinstance(audited_merge_sha, str)
                or audited_merge_sha.lower() != snapshot.merge_commit_oid
            ):
                return {
                    "status": "operator_required",
                    "reason": "merged_commit_audit_conflicts_with_readback",
                }
            if audited_merge_sha is not None and (
                result.get("status") != "merged"
                or result.get("pull_request_id")
                != f"{request.repository}#{pull_request_number}"
                or result.get("merge_action") != "default"
                or result.get("head_sha") != expected_head
            ):
                return {
                    "status": "operator_required",
                    "reason": "merged_result_audit_identity_mismatch",
                }
            if audited_merge_sha is None:
                merge_uuid = result.get("merge_request_id")
                if isinstance(merge_uuid, str) and isinstance(action.get("id"), str):
                    return self._poll_merge_handoff(
                        request,
                        pull_request_number,
                        pull_request_url,
                        expected_head,
                        cast(str, action["id"]),
                        merge_uuid,
                    )
                if action.get("status") not in {"pending", "uncertain"}:
                    return {
                        "status": "operator_required",
                        "reason": "merged_commit_audit_missing",
                    }
            merge_result = self._confirmed_merge_result(
                request, pull_request_number, expected_head, snapshot
            )
            if audited_merge_sha is None:
                _finish_handoff_mutation(
                    request,
                    cast(str, action["id"]),
                    "succeeded",
                    {"reconciliation": "merged_readback", **merge_result},
                )
            return merge_result

        if snapshot.is_draft is not False or snapshot.source_repository is None:
            return {
                "status": "operator_required",
                "reason": "pull_request_draft_or_source_repository_unproven",
            }
        if snapshot.source_repository != request.repository:
            return {
                "status": "operator_required",
                "reason": "pull_request_source_repository_mismatch",
            }

        if (
            authorization is None
            and action is not None
            and action.get("status") in {"pending", "uncertain", "succeeded"}
            and self._merge_target_matches_handoff(
                target,
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
            )
            and isinstance(target.get("review_cycle_id"), str)
        ):
            authorization = {
                "pull_request_id": f"{request.repository}#{pull_request_number}",
                "head_sha": expected_head,
                "cycle_id": target["review_cycle_id"],
                "approved_by_human": target.get("approved_by_human") == "true",
                "approval_actor": target.get("approval_actor"),
                "approval_reason": target.get("approval_reason"),
                "approval_at": target.get("approval_at"),
            }
        if authorization is None:
            return {
                "status": "operator_required",
                "reason": "current_review_authorization_required",
            }
        if (
            authorization.get("pull_request_id")
            != f"{request.repository}#{pull_request_number}"
            or authorization.get("head_sha") != expected_head
            or not isinstance(authorization.get("cycle_id"), str)
        ):
            return {
                "status": "operator_required",
                "reason": "review_authorization_identity_mismatch",
            }
        if (
            snapshot.state != "OPEN"
            or snapshot.source_branch != request.branch
            or snapshot.source_head != expected_head
            or snapshot.target_branch != request.base_branch
        ):
            return {
                "status": "operator_required",
                "reason": "pull_request_head_or_branch_changed",
            }
        recover_missing_uuid = False
        if action is not None:
            action_status = action.get("status")
            merge_uuid = result.get("merge_request_id")
            if not self._merge_target_matches_handoff(
                target,
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
            ) or target.get("review_cycle_id") != authorization.get("cycle_id"):
                return {
                    "status": "operator_required",
                    "reason": "previous_merge_target_changed",
                }
            if isinstance(merge_uuid, str) and action_status in {
                "pending",
                "uncertain",
                "succeeded",
            }:
                return self._poll_merge_handoff(
                    request,
                    pull_request_number,
                    pull_request_url,
                    expected_head,
                    cast(str, action["id"]),
                    merge_uuid,
                )
            if action_status in {"pending", "uncertain"}:
                action_request = action.get("request")
                attempt = (
                    cast(Mapping[str, object], action_request).get("attempt")
                    if isinstance(action_request, Mapping)
                    else None
                )
                if type(attempt) is not int:
                    return {
                        "status": "operator_required",
                        "reason": "merge_request_retry_count_unproven",
                    }
                if attempt >= 2:
                    return {"status": "deferred", "reason": "retry_limit_reached"}
                recover_missing_uuid = True
            if action_status == "succeeded":
                return {
                    "status": "operator_required",
                    "reason": "merge_readback_conflicts_with_audit",
                }
            if action_status == "failed":
                if result.get("retryable") is not True:
                    return {
                        "status": "operator_required",
                        "reason": "previous_merge_failure_not_retryable",
                    }
                action_request = action.get("request")
                attempt = (
                    cast(Mapping[str, object], action_request).get("attempt")
                    if isinstance(action_request, Mapping)
                    else None
                )
                retry_at = result.get("retry_after_at")
                if isinstance(retry_at, (int, float)) and time.time() < retry_at:
                    return {"status": "deferred", "retry_after_at": retry_at}
                if type(attempt) is int and attempt >= 2:
                    return {"status": "deferred", "reason": "retry_limit_reached"}
        if snapshot.conflict_state != "clean":
            return {
                "status": "operator_required",
                "reason": "pull_request_conflict_state_unproven",
            }
        checks = self._complete_pull_request_checks(
            *self._repository_parts(request.repository),
            pull_request_number,
            snapshot.source_head,
        )
        if (
            checks.get("head_sha") != expected_head
            or checks.get("verdict") != "passing"
        ):
            return {
                "status": "operator_required",
                "reason": "current_head_checks_not_passing",
            }

        merge_target = {
            "pull_request_number": str(pull_request_number),
            "pull_request_url": pull_request_url,
            "head_sha": expected_head,
            "branch": request.branch,
            "base_branch": cast(str, request.base_branch),
            "review_cycle_id": cast(str, authorization["cycle_id"]),
            "approved_by_human": str(
                authorization.get("approved_by_human", False)
            ).lower(),
        }
        for source, destination in (
            ("approval_actor", "approval_actor"),
            ("approval_reason", "approval_reason"),
            ("approval_at", "approval_at"),
        ):
            value = authorization.get(source)
            if isinstance(value, str):
                merge_target[destination] = value
        if recover_missing_uuid and not authorization_was_supplied:
            return {
                "status": "operator_required",
                "reason": "current_review_authorization_required",
            }
        if recover_missing_uuid and action is not None:
            prior_action_id = action.get("id")
            if not isinstance(prior_action_id, str):
                return {
                    "status": "operator_required",
                    "reason": "merge_audit_id_missing",
                }
            _finish_handoff_mutation(
                request,
                prior_action_id,
                "failed",
                {
                    "reconciliation": "merge_request_uuid_recovery_retry",
                    "retryable": True,
                },
            )
        action_id = _begin_handoff_mutation(
            request, "merge_pull_request", operation_key, merge_target
        )
        if not isinstance(action_id, str):
            return {"status": "operator_required", "reason": "merge_audit_id_missing"}
        owner, name = self._repository_parts(request.repository)
        rest_repo = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            status, payload = self._rest_request(
                "PUT",
                f"{rest_repo}/pulls/{pull_request_number}/merge-async",
                {"sha": expected_head, "merge_action": "default"},
            )
        except ProviderError as exc:
            try:
                observed = self.get_pull_request(
                    request.repository, pull_request_number
                )
            except ProviderError as read_error:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {
                        "reconciliation": "readback_unavailable",
                        "error_class": type(read_error).__name__,
                    },
                )
                return {
                    "status": self._provider_failure_status(read_error),
                    "error_class": type(read_error).__name__,
                }
            if self._merge_snapshot_matches_handoff(
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
                observed,
            ):
                merge_result = self._confirmed_merge_result(
                    request, pull_request_number, expected_head, observed
                )
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "succeeded",
                    {"reconciliation": "merged_readback", **merge_result},
                )
                return merge_result
            if observed.merged:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {"reconciliation": "merged_readback_conflicts"},
                )
                return {
                    "status": "operator_required",
                    "reason": "merged_readback_conflicts",
                }
            delay = self._retry_delay(exc)
            latest = self._handoff_action(request, "merge_pull_request", operation_key)
            attempt_number = 1
            if latest is not None:
                attempt_record = latest.get("request")
                if isinstance(attempt_record, Mapping):
                    raw_attempt = cast(Mapping[str, object], attempt_record).get(
                        "attempt"
                    )
                    if type(raw_attempt) is int:
                        attempt_number = raw_attempt
            retry_at = time.time() + delay if delay is not None else None
            _finish_handoff_mutation(
                request,
                action_id,
                "failed" if delay is not None else "uncertain",
                {
                    "reconciliation": "pull_request_not_merged",
                    "error_class": type(exc).__name__,
                    "retryable": delay is not None,
                    **({"retry_after_at": retry_at} if retry_at is not None else {}),
                },
            )
            if delay is not None and attempt_number == 1 and delay <= 1.0:
                if delay > 0:
                    time.sleep(delay)
                return self._merge_handoff(
                    request,
                    pull_request_number,
                    pull_request_url,
                    expected_head,
                    authorization,
                )
            return {
                "status": (
                    "deferred"
                    if delay is not None
                    or isinstance(
                        exc, (GitHubRateLimitError, GitHubOutcomeUnknownError)
                    )
                    else "operator_required"
                ),
                "error_class": type(exc).__name__,
                **({"retry_after_at": retry_at} if retry_at is not None else {}),
            }

        details = self._merge_details(payload)
        merge_uuid = details.get("uuid", payload.get("uuid"))
        merge_status = payload.get("status")
        if status == 200 and merge_status == "merged":
            observed = self.get_pull_request(request.repository, pull_request_number)
            response_sha = details.get("sha")
            if (
                not self._merge_snapshot_matches_handoff(
                    request,
                    pull_request_number,
                    pull_request_url,
                    expected_head,
                    observed,
                )
                or not isinstance(response_sha, str)
                or response_sha.lower() != observed.merge_commit_oid
            ):
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {"reconciliation": "merge_response_not_confirmed"},
                )
                return {
                    "status": "operator_required" if observed.merged else "deferred",
                    "reason": "merge_readback_incomplete",
                }
            merge_result = self._confirmed_merge_result(
                request, pull_request_number, expected_head, observed
            )
            _finish_handoff_mutation(
                request,
                action_id,
                "succeeded",
                {"reconciliation": "merged_readback", **merge_result},
            )
            return merge_result
        if (
            status in {200, 202, 409}
            and merge_status in {None, "pending"}
            and isinstance(merge_uuid, str)
        ):
            merge_action = details.get("merge_action")
            expected_head_sha = details.get("expected_head_sha")
            if (
                details.get("uuid", payload.get("uuid")) != merge_uuid
                or merge_action != "default"
                or expected_head_sha != expected_head
            ):
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "failed",
                    {
                        "reconciliation": "merge_request_policy_mismatch",
                        "retryable": False,
                    },
                )
                return {
                    "status": "operator_required",
                    "reason": "merge_request_policy_or_head_mismatch",
                }
            _finish_handoff_mutation(
                request,
                action_id,
                "succeeded",
                {
                    "merge_request_id": merge_uuid,
                    "status": "pending",
                    "merge_action": "default",
                    **(
                        {"merge_method": details["merge_method"]}
                        if isinstance(details.get("merge_method"), str)
                        else {}
                    ),
                },
            )
            return self._poll_merge_handoff(
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
                action_id,
                merge_uuid,
            )
        if status in {200, 202, 409}:
            if merge_status == "failed":
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "failed",
                    {
                        "reconciliation": "merge_request_failed",
                        "retryable": False,
                    },
                )
                return {
                    "status": "operator_required",
                    "reason": "asynchronous_merge_not_confirmed",
                }
            _finish_handoff_mutation(
                request,
                action_id,
                "uncertain",
                {"reconciliation": "merge_request_identity_missing"},
            )
            return {"status": "deferred", "reason": "merge_request_identity_missing"}
        else:
            _finish_handoff_mutation(
                request,
                action_id,
                "failed" if status < 500 else "uncertain",
                {
                    "reconciliation": "merge_request_rejected",
                    "http_status": status,
                    "retryable": False,
                },
            )
            return {
                "status": "operator_required",
                "reason": "merge_request_rejected",
                "http_status": status,
            }

    def _poll_merge_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        action_id: str,
        merge_uuid: str,
    ) -> dict[str, object]:
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            merge_uuid,
        ):
            return {"status": "operator_required", "reason": "invalid_merge_request_id"}
        owner, name = self._repository_parts(request.repository)
        rest_repo = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            status, payload = self._rest_request(
                "GET",
                f"{rest_repo}/pulls/{pull_request_number}/merge-async/{merge_uuid}",
            )
        except ProviderError as exc:
            return {
                "status": self._provider_failure_status(exc),
                "error_class": type(exc).__name__,
            }
        details = self._merge_details(payload)
        merge_state = payload.get("status")
        if (
            details.get("uuid") not in (None, merge_uuid)
            or details.get("expected_head_sha") not in (None, expected_head)
            or details.get("merge_action") not in (None, "default")
        ):
            return {
                "status": "operator_required",
                "reason": "merge_queue_identity_or_policy_mismatch",
            }
        if status == 200 and merge_state == "pending":
            if (
                details.get("uuid", payload.get("uuid")) != merge_uuid
                or details.get("expected_head_sha") != expected_head
                or details.get("merge_action") != "default"
            ):
                return {
                    "status": "operator_required",
                    "reason": "merge_queue_identity_or_policy_mismatch",
                }
            return {
                "status": "pending",
                "pull_request_id": f"{request.repository}#{pull_request_number}",
                "merge_request_id": merge_uuid,
                "merge_action": "default",
                **(
                    {"merge_method": details["merge_method"]}
                    if isinstance(details.get("merge_method"), str)
                    else {}
                ),
            }
        if status != 200 or merge_state != "merged":
            if status != 200 or merge_state != "failed":
                return {
                    "status": "operator_required",
                    "reason": "asynchronous_merge_result_unproven",
                }
            _finish_handoff_mutation(
                request,
                action_id,
                "failed",
                {
                    "reconciliation": "merge_request_failed",
                    "reason": details.get("message", "merge_request_failed"),
                    "retryable": False,
                },
            )
            return {
                "status": "operator_required",
                "reason": "asynchronous_merge_not_confirmed",
            }
        try:
            snapshot = self.get_pull_request(request.repository, pull_request_number)
        except ProviderError as exc:
            _finish_handoff_mutation(
                request,
                action_id,
                "uncertain",
                {
                    "reconciliation": "merge_readback_unavailable",
                    "error_class": type(exc).__name__,
                },
            )
            return {
                "status": self._provider_failure_status(exc),
                "error_class": type(exc).__name__,
            }
        response_sha = details.get("sha")
        if (
            not self._merge_snapshot_matches_handoff(
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
                snapshot,
            )
            or details.get("uuid", payload.get("uuid")) != merge_uuid
            or details.get("expected_head_sha") != expected_head
            or details.get("merge_action") != "default"
            or not isinstance(response_sha, str)
            or response_sha.lower() != snapshot.merge_commit_oid
        ):
            _finish_handoff_mutation(
                request,
                action_id,
                "uncertain",
                {"reconciliation": "merge_response_conflicts_with_readback"},
            )
            return {
                "status": "operator_required",
                "reason": "merge_response_conflicts_with_readback",
            }
        merge_result = self._confirmed_merge_result(
            request, pull_request_number, expected_head, snapshot
        )
        _finish_handoff_mutation(
            request,
            action_id,
            "succeeded",
            {"reconciliation": "merged_readback", **merge_result},
        )
        return merge_result

    def complete_approved_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """Merge only an authorized head, then reconcile issue, ref, and Project."""

        expected_head = expected_head.strip().lower()
        if (
            pull_request_number <= 0
            or not expected_head
            or not pull_request_url.strip()
            or request.base_branch is None
            or request.head_sha is None
            or expected_head != request.head_sha.strip().lower()
        ):
            return {
                "status": "operator_required",
                "reason": "persisted_handoff_identity_incomplete",
            }
        try:
            merge = self._merge_handoff(
                request,
                pull_request_number,
                pull_request_url,
                expected_head,
                authorization,
            )
        except ProviderError as exc:
            return {
                "status": self._provider_failure_status(exc),
                "step": "merge",
                "error_class": type(exc).__name__,
            }
        if merge.get("status") != "merged":
            return {**merge, "step": "merge"}

        prefix = f"{request.repository}#{request.pbi_number}"
        steps: dict[str, object] = {}
        issue_target = {"issue_number": str(request.pbi_number)}
        issue_state = self._ensure_completion_step(
            request,
            "close_issue",
            prefix,
            issue_target,
            lambda: self._issue_completion_state(request),
            lambda state: state.get("state") == "CLOSED",
            lambda state: state.get("state") == "OPEN",
            lambda state: self._execute_completion_mutation(
                CLOSE_ISSUE_MUTATION,
                {"input": {"issueId": state["issue_id"]}},
            ),
        )
        steps["issue"] = issue_state
        if issue_state.get("status") != "completed":
            return {
                "status": issue_state.get("status"),
                "step": "issue",
                "steps": steps,
            }

        expected_head = expected_head.strip().lower()
        ref_state = self._ensure_completion_step(
            request,
            "delete_ref",
            f"{request.repository}:{request.branch}:{expected_head}",
            {
                "branch": request.branch,
                "head_sha": expected_head,
            },
            lambda: self._source_ref_completion_state(request, expected_head),
            lambda state: state.get("exists") is False,
            lambda state: (
                state.get("exists") is True and state.get("head_sha") == expected_head
            ),
            lambda state: self._execute_completion_mutation(
                UPDATE_REFS_MUTATION,
                {
                    "input": {
                        "repositoryId": state["repository_id"],
                        "refUpdates": [
                            {
                                "name": state["ref_name"],
                                "beforeOid": expected_head,
                                "afterOid": "0" * 40,
                            }
                        ],
                    }
                },
            ),
        )
        steps["source_ref"] = ref_state
        if ref_state.get("status") != "completed":
            return {
                "status": ref_state.get("status"),
                "step": "source_ref",
                "steps": steps,
            }

        project_state = self._project_completion_state(request)
        if project_state.get("valid") is not True:
            return {
                "status": "operator_required",
                "step": "project_status",
                "reason": project_state.get("error", "Project state is unproven"),
                "steps": steps,
            }
        project_target = {
            "project_item_id": cast(str, project_state["item_id"]),
            "field_id": cast(str, project_state["field_id"]),
            "option_id": cast(str, project_state["done_option_id"]),
            "status": "Done",
        }
        project_result = self._ensure_completion_step(
            request,
            "update_project_status",
            prefix,
            project_target,
            lambda: self._project_completion_state(request),
            lambda state: state.get("status") == "Done",
            lambda state: state.get("status") == "In Progress",
            lambda state: self._execute_completion_mutation(
                UPDATE_PROJECT_STATUS_MUTATION,
                {
                    "input": {
                        "projectId": state["project_id"],
                        "itemId": state["item_id"],
                        "fieldId": state["field_id"],
                        "value": {"singleSelectOptionId": state["done_option_id"]},
                    }
                },
            ),
        )
        steps["project_status"] = project_result
        if project_result.get("status") != "completed":
            return {
                "status": project_result.get("status"),
                "step": "project_status",
                "steps": steps,
            }
        return {
            **merge,
            "status": "completed",
            "merged": True,
            "pull_request_url": pull_request_url,
            "head_sha": expected_head,
            "steps": steps,
        }

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
        if not request.run_id.strip():
            raise ProviderError("Handoff run identity is required")
        head_sha = request.head_sha
        if head_sha is not None:
            normalized_head = head_sha.strip().lower()
            if len(normalized_head) not in {40, 64} or any(
                char not in "0123456789abcdef" for char in normalized_head
            ):
                raise ProviderError("Verified handoff head must be a Git object ID")
            if not request.verification_evidence.strip():
                raise ProviderError("Verified handoff evidence is required")
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
        identity_marker = _handoff_marker(request, base_branch)
        pull_request_body = _handoff_body(request.body, identity_marker, request)
        legacy_marker = _legacy_handoff_marker(request)
        legacy_body = _handoff_body(request.body, legacy_marker, request)
        rate_limit_retries_used = 0
        previous_secondary_wait: float | None = None

        raw_branch_ref = repository.get("ref")
        branch_ref = _mapping(raw_branch_ref) if raw_branch_ref is not None else None
        expected_branch_oid = head_sha.strip() if head_sha is not None else base_oid
        branch_matches = branch_ref is not None and _branch_ref_matches(
            branch_ref, qualified_branch, expected_branch_oid
        )
        branch_result: dict[str, object] = {
            "reconciliation": (
                "present"
                if branch_matches
                else "absent"
                if branch_ref is None
                else "conflict"
            ),
            "branch": request.branch,
        }
        target_ref = branch_ref.get("target") if branch_ref is not None else None
        observed_branch_oid: object = None
        if isinstance(target_ref, Mapping):
            observed_branch_oid = cast(Mapping[str, object], target_ref).get("oid")
        if isinstance(observed_branch_oid, str):
            branch_result["head_sha"] = observed_branch_oid
        if branch_ref is not None:
            _reconcile_handoff_mutation(
                request,
                "create_ref",
                identity_marker,
                "succeeded" if branch_matches else "failed",
                branch_result,
            )

        if head_sha is not None:
            if not branch_matches:
                raise ProviderError(
                    "GitHub branch head does not match the verified pushed head"
                )
        elif branch_ref is not None:
            # ponytail: refs lack run metadata. Run-qualified branches or durable
            # identity are needed to prove orphan ownership.
            if not branch_matches:
                raise ProviderError(
                    "GitHub branch head does not match the intended base commit"
                )
        else:
            while True:
                action_id = _begin_handoff_mutation(
                    request,
                    "create_ref",
                    identity_marker,
                    {
                        "branch": request.branch,
                        "base_branch": base_branch,
                        "base_sha": base_oid,
                    },
                )
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
                    rate_limit_error = (
                        create_error
                        if isinstance(create_error, GitHubRateLimitError)
                        else None
                    )
                    if rate_limit_error is not None:
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            "uncertain",
                            {
                                "reconciliation": "awaiting_rate_limit_readback",
                                **_mutation_error_result(create_error),
                            },
                        )
                        delay = _rate_limit_wait_seconds(
                            rate_limit_error,
                            previous_secondary_wait=previous_secondary_wait,
                        )
                        if not rate_limit_error.primary:
                            previous_secondary_wait = delay
                        if delay > 0:
                            time.sleep(delay)
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
                        retry_repository = _mapping(
                            _mapping(retry_data.get("repository"))
                        )
                    except ProviderError:
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            "uncertain",
                            {
                                "reconciliation": "readback_unavailable",
                                **_mutation_error_result(create_error),
                            },
                        )
                        raise create_error from None
                    if (
                        not isinstance(retry_repository.get("id"), str)
                        or "ref" not in retry_repository
                    ):
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            "uncertain",
                            {
                                "reconciliation": "readback_unavailable",
                                **_mutation_error_result(create_error),
                            },
                        )
                        raise create_error from None
                    raw_retry_ref = retry_repository.get("ref")
                    if raw_retry_ref is None:
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            (
                                "uncertain"
                                if isinstance(create_error, GitHubOutcomeUnknownError)
                                else "failed"
                            ),
                            {
                                "reconciliation": "readback_absent",
                                "branch": request.branch,
                                **_mutation_error_result(create_error),
                            },
                        )
                        if (
                            rate_limit_error is not None
                            and rate_limit_retries_used < HANDOFF_RATE_LIMIT_RETRIES
                        ):
                            rate_limit_retries_used += 1
                            continue
                        raise create_error from None
                    retry_ref = _mapping(raw_retry_ref)
                    if not _branch_ref_matches(retry_ref, qualified_branch, base_oid):
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            "failed",
                            {
                                "reconciliation": "readback_conflict",
                                "branch": request.branch,
                                **_mutation_error_result(create_error),
                            },
                        )
                        raise ProviderError(
                            "GitHub branch head does not match the intended base commit"
                        ) from create_error
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {
                            "reconciliation": "readback_present",
                            "branch": request.branch,
                            "head_sha": base_oid,
                            **_mutation_error_result(create_error),
                        },
                    )
                    repository = retry_repository
                    break
                except Exception as create_error:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "uncertain",
                        {
                            "reconciliation": "not_checked",
                            **_mutation_error_result(create_error),
                        },
                    )
                    raise
                else:
                    raw_create_ref = create_data.get("createRef")
                    created_ref: Mapping[str, Any] = {}
                    response_invalid = not isinstance(raw_create_ref, Mapping)
                    if isinstance(raw_create_ref, Mapping):
                        raw_created_ref = cast(
                            Mapping[str, object], raw_create_ref
                        ).get("ref")
                        if isinstance(raw_created_ref, Mapping):
                            created_ref = cast(Mapping[str, Any], raw_created_ref)
                        else:
                            response_invalid = True
                    if response_invalid or not _branch_ref_matches(
                        created_ref, qualified_branch, base_oid
                    ):
                        error = ProviderError(
                            "GitHub GraphQL returned an invalid object"
                            if response_invalid
                            else (
                                "GitHub did not confirm branch creation: "
                                f"{qualified_branch}"
                            )
                        )
                        _finish_handoff_mutation(
                            request,
                            action_id,
                            "uncertain",
                            {
                                "reconciliation": "response_unverified",
                                "branch": request.branch,
                                **_mutation_error_result(error),
                            },
                        )
                        raise error
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {
                            "reconciliation": "mutation_response",
                            "branch": request.branch,
                            "head_sha": base_oid,
                        },
                    )
                    break

        existing = self._find_existing_pull_request(
            owner,
            name,
            qualified_branch,
            request.branch,
            base_branch,
            identity_marker,
            legacy_marker=legacy_marker,
            legacy_body=legacy_body,
            initial_repository=repository,
        )
        if existing is not None:
            existing_matches = _pull_request_matches(
                existing,
                request.branch,
                base_branch,
                identity_marker,
                legacy_marker,
                legacy_body,
            )
            _reconcile_handoff_mutation(
                request,
                "create_pull_request",
                identity_marker,
                "succeeded" if existing_matches else "failed",
                _pull_request_audit_result(
                    existing, "present" if existing_matches else "conflict"
                ),
            )
            return self._update_matching_pull_request(
                existing,
                owner,
                name,
                qualified_branch,
                request,
                base_branch,
                identity_marker,
                legacy_marker,
                legacy_body,
                pull_request_body,
            )
        while True:
            action_id = _begin_handoff_mutation(
                request,
                "create_pull_request",
                identity_marker,
                {
                    "branch": request.branch,
                    "base_branch": base_branch,
                    **({"head_sha": head_sha} if head_sha is not None else {}),
                },
            )
            try:
                pull_request_data = self._client.execute(
                    CREATE_PULL_REQUEST_MUTATION,
                    {
                        "input": {
                            "repositoryId": repository_id,
                            "baseRefName": base_branch,
                            "headRefName": request.branch,
                            "title": request.title,
                            "body": pull_request_body,
                            "draft": True,
                        }
                    },
                )
            except ProviderError as create_error:
                rate_limit_error = (
                    create_error
                    if isinstance(create_error, GitHubRateLimitError)
                    else None
                )
                if rate_limit_error is not None:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "uncertain",
                        {
                            "reconciliation": "awaiting_rate_limit_readback",
                            **_mutation_error_result(create_error),
                        },
                    )
                    delay = _rate_limit_wait_seconds(
                        rate_limit_error,
                        previous_secondary_wait=previous_secondary_wait,
                    )
                    if not rate_limit_error.primary:
                        previous_secondary_wait = delay
                    if delay > 0:
                        time.sleep(delay)
                try:
                    existing = self._find_existing_pull_request(
                        owner,
                        name,
                        qualified_branch,
                        request.branch,
                        base_branch,
                        identity_marker,
                        legacy_marker=legacy_marker,
                        legacy_body=legacy_body,
                    )
                except ProviderError:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "uncertain",
                        {
                            "reconciliation": "readback_unavailable",
                            **_mutation_error_result(create_error),
                        },
                    )
                    raise create_error from None
                if existing is None:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        (
                            "uncertain"
                            if isinstance(create_error, GitHubOutcomeUnknownError)
                            else "failed"
                        ),
                        {
                            "reconciliation": "readback_absent",
                            **_mutation_error_result(create_error),
                        },
                    )
                    if (
                        rate_limit_error is not None
                        and rate_limit_retries_used < HANDOFF_RATE_LIMIT_RETRIES
                    ):
                        rate_limit_retries_used += 1
                        continue
                    raise create_error from None
                if not _pull_request_matches(
                    existing,
                    request.branch,
                    base_branch,
                    identity_marker,
                    legacy_marker,
                    legacy_body,
                ):
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "failed",
                        {
                            **_pull_request_audit_result(existing, "readback_conflict"),
                            **_mutation_error_result(create_error),
                        },
                    )
                    raise ProviderError(
                        "Pull request branch has a different handoff identity"
                    ) from create_error
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "succeeded",
                    {
                        **_pull_request_audit_result(existing, "readback_present"),
                        **_mutation_error_result(create_error),
                    },
                )
                return self._update_matching_pull_request(
                    existing,
                    owner,
                    name,
                    qualified_branch,
                    request,
                    base_branch,
                    identity_marker,
                    legacy_marker,
                    legacy_body,
                    pull_request_body,
                )
            except Exception as create_error:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {
                        "reconciliation": "not_checked",
                        **_mutation_error_result(create_error),
                    },
                )
                raise
            raw_create_result = pull_request_data.get("createPullRequest")
            pull_request: Mapping[str, Any] = {}
            response_invalid = not isinstance(raw_create_result, Mapping)
            if isinstance(raw_create_result, Mapping):
                raw_pull_request = cast(Mapping[str, object], raw_create_result).get(
                    "pullRequest"
                )
                if isinstance(raw_pull_request, Mapping):
                    pull_request = cast(Mapping[str, Any], raw_pull_request)
                else:
                    response_invalid = True
            try:
                if response_invalid:
                    raise ProviderError("GitHub GraphQL returned an invalid object")
                result = self._validated_handoff_result(
                    pull_request,
                    request,
                    base_branch,
                    identity_marker,
                    pull_request_body,
                )
            except ProviderError as validation_error:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {
                        **_pull_request_audit_result(
                            pull_request, "response_unverified"
                        ),
                        **_mutation_error_result(validation_error),
                    },
                )
                raise
            _finish_handoff_mutation(
                request,
                action_id,
                "succeeded",
                {
                    **_pull_request_audit_result(pull_request, "mutation_response"),
                    "branch": result.branch,
                },
            )
            return result

    def _validated_handoff_result(
        self,
        pull_request: Mapping[str, Any],
        request: HandoffRequest,
        base_branch: str,
        identity_marker: str,
        expected_body: str,
    ) -> HandoffResult:
        if not pull_request:
            raise ProviderError("GitHub did not return a pull-request record")
        if not _pull_request_matches(
            pull_request, request.branch, base_branch, identity_marker
        ):
            raise ProviderError(
                "GitHub returned a pull request for a different handoff"
            )
        number = pull_request.get("number")
        url = _required_text(pull_request.get("url"), "pull-request URL")
        pull_request_id = _required_text(pull_request.get("id"), "pull-request id")
        state = pull_request.get("state")
        if state != "OPEN":
            raise ProviderError(
                f"Matching pull request #{number} is not open; refusing to update it"
            )
        if pull_request.get("isDraft") is not True:
            raise ProviderError(
                f"Matching pull request #{number} is not a draft; refusing to update it"
            )
        if (
            not isinstance(number, int)
            or number <= 0
            or url is None
            or pull_request_id is None
            or pull_request.get("title") != request.title
            or pull_request.get("body") != expected_body
        ):
            raise ProviderError("GitHub did not confirm the pull-request handoff")
        if request.head_sha is not None:
            pull_request_head = pull_request.get("headRefOid")
            if (
                not isinstance(pull_request_head, str)
                or pull_request_head.lower() != request.head_sha.strip().lower()
            ):
                raise ProviderError(
                    "Pull-request head does not match the verified pushed head"
                )
        return HandoffResult(request.branch, url, number)

    def _update_matching_pull_request(
        self,
        existing: Mapping[str, Any],
        owner: str,
        name: str,
        qualified_branch: str,
        request: HandoffRequest,
        base_branch: str,
        identity_marker: str,
        legacy_marker: str | None,
        legacy_body: str | None,
        pull_request_body: str,
    ) -> HandoffResult:
        if not _pull_request_matches(
            existing,
            request.branch,
            base_branch,
            identity_marker,
            legacy_marker,
            legacy_body,
        ):
            raise ProviderError(
                "An existing pull request has a different handoff identity; "
                "refusing to update it"
            )
        current = existing
        for attempt in range(2):
            self._validated_handoff_identity(
                current,
                request,
                base_branch,
                identity_marker,
                legacy_marker,
                legacy_body,
            )
            pull_request_id = _required_text(current.get("id"), "pull-request id")
            if pull_request_id is None:
                raise ProviderError("GitHub returned incomplete pull-request identity")
            try:
                update_data = self._client.execute(
                    UPDATE_PULL_REQUEST_MUTATION,
                    {
                        "input": {
                            "pullRequestId": pull_request_id,
                            "title": request.title,
                            "body": pull_request_body,
                        }
                    },
                )
            except ProviderError as update_error:
                if attempt:
                    raise
                try:
                    latest = self._find_existing_pull_request(
                        owner,
                        name,
                        qualified_branch,
                        request.branch,
                        base_branch,
                        identity_marker,
                        legacy_marker=legacy_marker,
                        legacy_body=legacy_body,
                    )
                except ProviderError:
                    raise update_error from None
                if latest is None:
                    raise update_error from None
                self._validated_handoff_identity(
                    latest,
                    request,
                    base_branch,
                    identity_marker,
                    legacy_marker,
                    legacy_body,
                )
                if (
                    latest.get("title") == request.title
                    and latest.get("body") == pull_request_body
                ):
                    return self._validated_handoff_result(
                        latest,
                        request,
                        base_branch,
                        identity_marker,
                        pull_request_body,
                    )
                current = latest
                continue
            updated = _mapping(
                _mapping(update_data.get("updatePullRequest")).get("pullRequest")
            )
            return self._validated_handoff_result(
                updated, request, base_branch, identity_marker, pull_request_body
            )
        raise ProviderError("GitHub could not update the matching draft pull request")

    def _validated_handoff_identity(
        self,
        pull_request: Mapping[str, Any],
        request: HandoffRequest,
        base_branch: str,
        identity_marker: str,
        legacy_marker: str | None = None,
        legacy_body: str | None = None,
    ) -> None:
        if not _pull_request_matches(
            pull_request,
            request.branch,
            base_branch,
            identity_marker,
            legacy_marker,
            legacy_body,
        ):
            raise ProviderError(
                "An existing pull request has a different handoff identity; "
                "refusing to update it"
            )
        number = pull_request.get("number")
        if pull_request.get("state") != "OPEN":
            raise ProviderError(
                f"Matching pull request #{number} is not open; refusing to update it"
            )
        if pull_request.get("isDraft") is not True:
            raise ProviderError(
                f"Matching pull request #{number} is not a draft; refusing to update it"
            )
        if request.head_sha is not None:
            pull_request_head = pull_request.get("headRefOid")
            if (
                not isinstance(pull_request_head, str)
                or pull_request_head.lower() != request.head_sha.strip().lower()
            ):
                raise ProviderError(
                    "Pull-request head does not match the verified pushed head"
                )

    def _find_existing_pull_request(
        self,
        owner: str,
        name: str,
        qualified_branch: str,
        branch: str,
        base_branch: str,
        identity_marker: str,
        legacy_marker: str | None = None,
        legacy_body: str | None = None,
        initial_repository: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        cursor: str | None = None
        repository = initial_repository
        matching: Mapping[str, Any] | None = None
        conflicting: Mapping[str, Any] | None = None
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
            for raw_pull_request in _nodes(repository.get("pullRequests", {})):
                pull_request = _mapping(raw_pull_request)
                if (
                    pull_request.get("headRefName") != branch
                    or pull_request.get("baseRefName") != base_branch
                ):
                    continue
                if _pull_request_matches(
                    pull_request,
                    branch,
                    base_branch,
                    identity_marker,
                    legacy_marker,
                    legacy_body,
                ):
                    if matching is not None:
                        raise ProviderError(
                            "More than one pull request matches this handoff identity"
                        )
                    matching = pull_request
                elif conflicting is None:
                    conflicting = pull_request
            has_next, cursor = _next_cursor(
                _mapping(repository.get("pullRequests", {}))
            )
            if not has_next:
                return matching or conflicting
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

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        provider = cast(PbiCreationProvider, self._configured_provider())
        return provider.prepare_pbi_creation(request)

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        provider = cast(PbiCreationProvider, self._configured_provider())
        return provider.create_pbi(request, target, progress, checkpoint)

    def apply_pbi_refinement(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult:
        provider = cast(PbiRefinementMutationProvider, self._configured_provider())
        return provider.apply_pbi_refinement(request)

    def prepare_pbi_relations(self, request: PbiRelationRequest) -> PbiRelationSnapshot:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.prepare_pbi_relations(request)

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_sub_issues(repository, parent_issue_number)

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.get_pbi_parent_issue_number(repository, child_issue_number)

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_blocked_by(repository, issue_number)

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_blocking(repository, issue_number)

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        provider.add_pbi_sub_issue(repository, parent_issue_number, child_issue_id)

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        provider.add_pbi_dependency(repository, blocked_issue_number, blocker_issue_id)

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

    def complete_approved_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        return self._configured_provider().complete_approved_handoff(
            request,
            pull_request_number,
            pull_request_url,
            expected_head,
            authorization,
        )

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
