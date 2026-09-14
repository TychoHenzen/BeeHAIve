from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote

from beehaiive.github.errors.outcome_unknown_error import (
    GitHubOutcomeUnknownError as GitHubOutcomeUnknownError,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.handoff_helpers import (
    _begin_handoff_mutation as _begin_handoff_mutation,
)
from beehaiive.github.handoff_helpers import (
    _finish_handoff_mutation as _finish_handoff_mutation,
)
from beehaiive.models import HandoffRequest


# ponytail: keep merge reconciliation whole until replay tests allow a split.
class HandoffMergeMixin:
    def _merge_handoff(
        self: Any,
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


__all__ = ["HandoffMergeMixin"]
