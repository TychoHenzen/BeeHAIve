from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_helpers import _commit_oid as _commit_oid
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import (
    _pull_request_head_sha as _pull_request_head_sha,
)
from beehaiive.github.graphql_helpers import _required_text as _required_text
from beehaiive.models import PullRequestSnapshot


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


__all__ = ["_pull_request_snapshot"]
