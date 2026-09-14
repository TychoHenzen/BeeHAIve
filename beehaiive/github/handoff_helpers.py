from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.models import HandoffRequest


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


__all__ = [
    "_validate_branch_name",
    "_handoff_marker",
    "_begin_handoff_mutation",
    "_finish_handoff_mutation",
    "_reconcile_handoff_mutation",
    "_mutation_error_result",
    "_pull_request_audit_result",
    "_legacy_handoff_marker",
    "_handoff_body",
    "_pull_request_matches",
    "_branch_ref_matches",
]
