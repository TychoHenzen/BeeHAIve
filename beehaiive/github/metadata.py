from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from beehaiive.checks import normalize_check_rollup
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import _commit_oid as _commit_oid
from beehaiive.github.graphql_helpers import (
    _complete_connection as _complete_connection,
)
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import (
    _pull_request_head_sha as _pull_request_head_sha,
)
from beehaiive.github.queries.completion import (
    PULL_REQUEST_CHECKS_QUERY as PULL_REQUEST_CHECKS_QUERY,
)
from beehaiive.github.queries.discovery import (
    ISSUE_COMMENTS_QUERY as ISSUE_COMMENTS_QUERY,
)
from beehaiive.github.queries.discovery import ISSUE_LABELS_QUERY as ISSUE_LABELS_QUERY
from beehaiive.github.queries.discovery import (
    ISSUE_PULL_REQUESTS_QUERY as ISSUE_PULL_REQUESTS_QUERY,
)
from beehaiive.github.queries.discovery import (
    ISSUE_SUB_ISSUES_QUERY as ISSUE_SUB_ISSUES_QUERY,
)
from beehaiive.github.queries.pull_requests import (
    PULL_REQUEST_LATEST_REVIEWS_QUERY as PULL_REQUEST_LATEST_REVIEWS_QUERY,
)
from beehaiive.github.queries.pull_requests import (
    PULL_REQUEST_REVIEW_REQUESTS_QUERY as PULL_REQUEST_REVIEW_REQUESTS_QUERY,
)
from beehaiive.pbi_relations import MAX_PBI_RELATION_CHILDREN, PbiRelationProviderError


class MetadataMixin:
    def _complete_issue_metadata(
        self: Any,
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
        self: Any,
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


__all__ = ["MetadataMixin"]
