from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _required_text as _required_text
from beehaiive.github.handoff_helpers import (
    _pull_request_matches as _pull_request_matches,
)
from beehaiive.github.queries.pull_requests import REPOSITORY_QUERY as REPOSITORY_QUERY
from beehaiive.github.queries.pull_requests import (
    UPDATE_PULL_REQUEST_MUTATION as UPDATE_PULL_REQUEST_MUTATION,
)
from beehaiive.models import HandoffRequest, HandoffResult


class HandoffResultMixin:
    def _validated_handoff_result(
        self: Any,
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
        self: Any,
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
        self: Any,
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
        self: Any,
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


__all__ = ["HandoffResultMixin"]
