from __future__ import annotations

from collections.abc import Mapping, Sequence

from .stages import display_stage, display_stage_label, stage_progress
from .values import integer, mapping, mappings, sequence


def repository_view(
    raw_repository: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
    include_archived: bool,
) -> dict[str, object]:
    raw_writer = mapping(raw_repository.get("writer"))
    if raw_writer:
        writer: dict[str, object] = {
            **raw_writer,
            "status": "active",
            "current_pbi": raw_writer.get("pbi_number"),
        }
    else:
        writer = {"status": "idle", "current_pbi": None}

    pbis = [
        pbi_view(raw_pbi, raw_repository.get("name"), actions)
        for raw_pbi in mappings(raw_repository.get("pbis"))
        if bool(raw_pbi.get("archived")) is include_archived
    ]
    return {
        "name": raw_repository.get("name"),
        "active": bool(raw_repository.get("active")),
        "writer": writer,
        "pbis": pbis,
        "counts": {
            "pbis": len(pbis),
            "subtasks": sum(len(sequence(pbi.get("subtasks"))) for pbi in pbis),
            "readers": sum(
                len(sequence(pbi.get("readers"))) or len(mapping(pbi.get("reviewers")))
                for pbi in pbis
            ),
        },
    }


def pbi_view(
    raw_pbi: Mapping[str, object],
    repository_name: object,
    actions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    events = mappings(raw_pbi.get("events"))
    metadata = dict(mapping(raw_pbi.get("metadata")))
    metadata.update(latest_metadata(events))
    raw_stage = str(raw_pbi.get("stage") or "backlog")
    raw_status = raw_pbi.get("status")
    status = str(raw_status) if raw_status is not None else "idle"
    planning_status = raw_pbi.get("planning_status")
    checks = dict(mapping(metadata.get("checks")))
    pull_requests = sequence(metadata.get("pull_requests"))
    display_stage_value = display_stage(
        raw_stage, status, planning_status, pull_requests
    )
    readers = sequence(metadata.get("readers"))
    reviewers = dict(mapping(metadata.get("reviewers")))
    if not readers and reviewers:
        readers = [
            {"id": reviewer_id, **mapping(reviewer)}
            for reviewer_id, reviewer in reviewers.items()
        ]
    escalation = escalation_view(metadata, events)
    subtasks = sequence(metadata.get("subtasks"))
    dependency_readiness = mapping(metadata.get("dependency_readiness")) or None
    source_url = metadata.get("source_url")
    matching_actions = [
        dict(action)
        for action in actions
        if action_matches(action, repository_name, raw_pbi)
    ]
    return {
        "id": raw_pbi.get("id"),
        "number": raw_pbi.get("number"),
        "title": raw_pbi.get("title"),
        "stage": raw_stage,
        "stage_label": display_stage_label(display_stage_value, planning_status),
        "stage_progress": stage_progress(display_stage_value, planning_status),
        "status": status,
        "attempt": raw_pbi.get("attempt"),
        "run_id": raw_pbi.get("run_id"),
        "branch": raw_pbi.get("branch"),
        "pull_request_url": raw_pbi.get("pull_request_url"),
        "last_error": raw_pbi.get("last_error"),
        "result": raw_pbi.get("result"),
        "task_contract": mapping(raw_pbi.get("task_contract")) or None,
        "task_result": mapping(raw_pbi.get("task_result")) or None,
        "task_answer": raw_pbi.get("task_answer"),
        "operator_questions": sequence(raw_pbi.get("operator_questions")),
        "agent_session": mapping(raw_pbi.get("agent_session")) or None,
        "active": bool(raw_pbi.get("active")),
        "archived": bool(raw_pbi.get("archived")),
        "planning_status": planning_status,
        "issue_state": metadata.get("issue_state"),
        "state_reason": metadata.get("state_reason"),
        "source_url": source_url,
        "claimable": bool(raw_pbi.get("claimable")),
        "checks": checks,
        "subtasks": subtasks,
        "dependency_readiness": dependency_readiness,
        "pull_requests": pull_requests,
        "readers": readers,
        "reviewers": reviewers,
        "escalation": escalation[0],
        "escalation_log": escalation[1],
        "activity": [*(sequence(metadata.get("activity"))), *events],
        "events": events,
        "operator_actions": matching_actions,
    }


def latest_metadata(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for event in events:
        details = mapping(event.get("details"))
        for key in (
            "readers",
            "reviewers",
            "subtasks",
            "dependency_readiness",
            "checks",
            "escalation",
            "escalation_log",
        ):
            if key in details:
                metadata[key] = details[key]
    return metadata


def escalation_view(
    metadata: Mapping[str, object], events: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], list[object]]:
    raw_escalation = mapping(metadata.get("escalation"))
    consecutive = integer(raw_escalation.get("consecutive"))
    current = integer(raw_escalation.get("current"))
    current_tier = raw_escalation.get("current_tier")
    escalation: dict[str, object] = {
        "current": current,
        "consecutive": consecutive,
    }
    if isinstance(current_tier, str) and current_tier.strip():
        escalation["current_tier"] = current_tier
    raw_log: list[object] = list(sequence(metadata.get("escalation_log")))
    if not raw_log:
        raw_log = [
            mapping(event.get("details"))
            for event in events
            if event.get("type") in {"escalation", "triage"}
        ]
    return escalation, raw_log


def action_matches(
    action: Mapping[str, object],
    repository_name: object,
    raw_pbi: Mapping[str, object],
) -> bool:
    action_repository = action.get("repository")
    action_number = action.get("pbi_number")
    return action_repository == repository_name and action_number == raw_pbi.get(
        "number"
    )
