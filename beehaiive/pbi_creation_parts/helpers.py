from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .pbi_creation_request import PbiCreationRequest


def _request_fingerprint(request: PbiCreationRequest) -> str:
    payload = json.dumps(
        {
            "project_id": request.project_id,
            "repository": request.repository,
            "title": request.title,
            "body": request.body,
            "labels": list(request.labels),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _incomplete_result(record: Mapping[str, object]) -> dict[str, object]:
    issue_number = record.get("issue_number")
    issue_url = record.get("issue_url")
    issue: dict[str, object] | None = None
    if type(issue_number) is int and isinstance(issue_url, str):
        issue = {
            "id": record.get("issue_id"),
            "number": issue_number,
            "url": issue_url,
        }
    status = record.get("status")
    result: dict[str, object] = {
        "status": status if isinstance(status, str) else "incomplete",
        "issue": issue,
        "completed_steps": record.get("completed_steps", []),
        "failed_step": record.get("failed_step") or record.get("current_step"),
        "operator_action_required": status == "outcome_unknown",
    }
    failure_code = record.get("failure_code")
    failure_class = record.get("failure_class")
    if isinstance(failure_code, str):
        result["failure"] = {
            "code": failure_code[:80],
            "type": failure_class[:80]
            if isinstance(failure_class, str)
            else "ProviderError",
        }
    return result


__all__ = ["_request_fingerprint", "_incomplete_result"]
