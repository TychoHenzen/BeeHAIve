from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.constants import (
    HANDOFF_RATE_LIMIT_RETRIES as HANDOFF_RATE_LIMIT_RETRIES,
)
from beehaiive.github.errors.outcome_unknown_error import (
    GitHubOutcomeUnknownError as GitHubOutcomeUnknownError,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.handoff_helpers import (
    _begin_handoff_mutation as _begin_handoff_mutation,
)
from beehaiive.github.handoff_helpers import _branch_ref_matches as _branch_ref_matches
from beehaiive.github.handoff_helpers import (
    _finish_handoff_mutation as _finish_handoff_mutation,
)
from beehaiive.github.handoff_helpers import _handoff_body as _handoff_body
from beehaiive.github.handoff_helpers import _handoff_marker as _handoff_marker
from beehaiive.github.handoff_helpers import (
    _legacy_handoff_marker as _legacy_handoff_marker,
)
from beehaiive.github.handoff_helpers import (
    _mutation_error_result as _mutation_error_result,
)
from beehaiive.github.handoff_helpers import (
    _pull_request_audit_result as _pull_request_audit_result,
)
from beehaiive.github.handoff_helpers import (
    _pull_request_matches as _pull_request_matches,
)
from beehaiive.github.handoff_helpers import (
    _reconcile_handoff_mutation as _reconcile_handoff_mutation,
)
from beehaiive.github.handoff_helpers import (
    _validate_branch_name as _validate_branch_name,
)
from beehaiive.github.queries.pull_requests import (
    CREATE_PULL_REQUEST_MUTATION as CREATE_PULL_REQUEST_MUTATION,
)
from beehaiive.github.queries.pull_requests import (
    CREATE_REF_MUTATION as CREATE_REF_MUTATION,
)
from beehaiive.github.queries.pull_requests import REPOSITORY_QUERY as REPOSITORY_QUERY
from beehaiive.github.transport_helpers import (
    _rate_limit_wait_seconds as _rate_limit_wait_seconds,
)
from beehaiive.models import HandoffRequest, HandoffResult


# ponytail: keep this idempotent create flow whole until replay tests allow a split.
class HandoffCreateMixin:
    def create_handoff(self: Any, request: HandoffRequest) -> HandoffResult:
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


__all__ = ["HandoffCreateMixin"]
