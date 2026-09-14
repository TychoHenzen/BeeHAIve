from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from beehaiive.checks import aggregate_check_verdict
from beehaiive.github.graphql_helpers import _actor_name as _actor_name
from beehaiive.github.graphql_helpers import _label_names as _label_names
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import (
    _pull_request_head_sha as _pull_request_head_sha,
)
from beehaiive.github.graphql_helpers import _review_status as _review_status
from beehaiive.pbi_readiness import project_pbi_readiness


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


__all__ = ["_dashboard_metadata"]
