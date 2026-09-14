"""Conservative readiness projection for decomposed PBIs."""

from collections.abc import Mapping, Sequence
from typing import cast

READINESS_OUTCOMES = (
    "ready",
    "incomplete",
    "blocked",
    "rejected",
    "completed",
    "unknown",
)


def classify_child_readiness(
    child: Mapping[str, object],
) -> tuple[str, list[str]]:
    """Classify one child without treating missing evidence as ready."""

    reasons: list[str] = []

    def unknown(reason: str) -> tuple[str, list[str]]:
        return "unknown", [reason]

    if child.get("source_stale") is True:
        return unknown("source_stale")
    if (
        not isinstance(child.get("observed_at"), str)
        or not str(child.get("observed_at")).strip()
    ):
        return unknown("observation_time_missing")
    if child.get("project_status_conflict") is True:
        return unknown("project_status_conflict")
    if child.get("dependency_read_complete") is not True:
        error = child.get("dependency_read_error")
        return unknown(
            error if isinstance(error, str) and error else "dependency_read_incomplete"
        )

    raw_state = child.get("issue_state")
    state = raw_state.casefold() if isinstance(raw_state, str) else ""
    raw_reason = child.get("state_reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        return unknown("state_reason_invalid")
    state_reason = raw_reason.casefold() if isinstance(raw_reason, str) else ""
    raw_status = child.get("project_status")
    project_status = raw_status.casefold() if isinstance(raw_status, str) else ""
    if state not in {"open", "closed"}:
        return unknown("issue_state_missing")
    if raw_status is not None and not project_status:
        return unknown("project_status_invalid")

    raw_blockers = child.get("blocked_by")
    if not isinstance(raw_blockers, Sequence) or isinstance(
        raw_blockers, (str, bytes, bytearray)
    ):
        return unknown("dependency_facts_invalid")

    active_blockers: list[str] = []
    for raw_blocker in cast(Sequence[object], raw_blockers):
        if not isinstance(raw_blocker, Mapping):
            return unknown("dependency_fact_invalid")
        blocker = cast(Mapping[str, object], raw_blocker)
        blocker_state = blocker.get("state")
        blocker_state = (
            blocker_state.casefold() if isinstance(blocker_state, str) else ""
        )
        raw_blocker_reason = blocker.get("state_reason")
        if raw_blocker_reason is not None and not isinstance(raw_blocker_reason, str):
            return unknown("blocker_state_reason_invalid")
        blocker_reason = (
            raw_blocker_reason.casefold() if isinstance(raw_blocker_reason, str) else ""
        )
        number = blocker.get("number")
        blocker_id = (
            f"#{number}" if isinstance(number, int) and number > 0 else "blocker"
        )
        if blocker_state == "open":
            if blocker_reason and blocker_reason != "reopened":
                return unknown("blocker_state_reason_conflict")
            active_blockers.append(f"blocked_by_open:{blocker_id}")
        elif blocker_state == "closed" and blocker_reason == "not_planned":
            active_blockers.append(f"blocked_by_rejected:{blocker_id}")
        elif blocker_state == "closed" and blocker_reason == "completed":
            continue
        else:
            return unknown("blocker_state_or_reason_unknown")

    if state == "closed":
        if state_reason not in {"completed", "not_planned"}:
            return unknown("closed_issue_reason_unknown")
        if project_status and project_status != "done":
            return unknown("closed_issue_project_status_conflict")
        if active_blockers:
            return unknown("closed_issue_has_unresolved_blocker")
        return ("completed" if state_reason == "completed" else "rejected"), reasons

    if state_reason and state_reason != "reopened":
        return unknown("open_issue_reason_conflict")
    if project_status in {"done", "completed", "closed", "merged"}:
        return unknown("open_issue_project_status_conflict")
    if active_blockers:
        return "blocked", active_blockers
    if project_status == "todo":
        return "ready", reasons
    if project_status in {"backlog", "in progress"}:
        return "incomplete", ["project_status_not_todo"]
    return unknown("project_status_unknown")


def project_pbi_readiness(
    children: Sequence[Mapping[str, object]],
    *,
    relation_complete: bool,
    relation_error: str | None,
    observed_at: str | None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Attach child outcomes and summarize the complete expected child set."""

    projected_children: list[dict[str, object]] = []
    counts: dict[str, int] = {outcome: 0 for outcome in READINESS_OUTCOMES}
    for child in children:
        projected = dict(child)
        if observed_at:
            projected["observed_at"] = observed_at
        status, reasons = classify_child_readiness(projected)
        projected["readiness"] = status
        projected["readiness_reasons"] = reasons
        counts[status] += 1
        projected_children.append(projected)

    reasons: list[str] = []
    if not isinstance(observed_at, str) or not observed_at.strip():
        reasons.append("observation_time_missing")
    if relation_error:
        reasons.append(relation_error)
    if not relation_complete:
        reasons.append("child_relation_incomplete")
    if not projected_children:
        reasons.append("no_linked_children")

    if reasons or counts["unknown"]:
        status = "unknown"
        if counts["unknown"]:
            reasons.append("child_evidence_unknown")
    elif counts["rejected"]:
        status = "rejected"
        reasons.append("child_rejected")
    elif counts["blocked"]:
        status = "blocked"
        reasons.append("child_blocked")
    elif counts["incomplete"]:
        status = "incomplete"
        reasons.append("child_incomplete")
    elif counts["completed"] == len(projected_children):
        status = "completed"
    else:
        status = "ready"

    return projected_children, {
        "status": status,
        "counts": counts,
        "reasons": reasons,
        "observed_at": observed_at,
    }
