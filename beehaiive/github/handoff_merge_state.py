from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.outcome_unknown_error import (
    GitHubOutcomeUnknownError as GitHubOutcomeUnknownError,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.graphql_helpers import _commit_oid as _commit_oid
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.completion import BRANCH_REF_QUERY as BRANCH_REF_QUERY
from beehaiive.github.queries.completion import (
    COMPLETION_PROJECT_QUERY as COMPLETION_PROJECT_QUERY,
)
from beehaiive.github.queries.completion import (
    ISSUE_COMPLETION_QUERY as ISSUE_COMPLETION_QUERY,
)
from beehaiive.models import HandoffRequest, PullRequestSnapshot


class HandoffMergeStateMixin:
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
        self: Any, query: str, variables: Mapping[str, object]
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
        self: Any, request: HandoffRequest, mutation: str, operation_key: str
    ) -> dict[str, object] | None:
        if request.mutation_audit is None:
            return None
        return request.mutation_audit.handoff_mutation_action(
            request, mutation, operation_key
        )

    def _rest_request(
        self: Any,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        request_rest = getattr(self._client, "request_rest", None)
        if not callable(request_rest):
            raise ProviderError("GitHub REST mutations are not configured")
        return cast(tuple[int, Mapping[str, Any]], request_rest(method, path, payload))


__all__ = ["HandoffMergeStateMixin"]
