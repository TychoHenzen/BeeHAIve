from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.handoff_helpers import (
    _finish_handoff_mutation as _finish_handoff_mutation,
)
from beehaiive.models import HandoffRequest


class HandoffPollMixin:
    def _poll_merge_handoff(
        self: Any,
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


__all__ = ["HandoffPollMixin"]
