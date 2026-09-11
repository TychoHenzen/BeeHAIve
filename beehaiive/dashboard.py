"""Read-only projection used by the live operator dashboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from .models import project_stage_from_status

_DISPLAY_STAGES = (
    "backlog",
    "refine",
    "implement",
    "review",
    "pull_request",
    "merge",
)
_DISPLAY_STAGE_LABELS = {
    "backlog": "Backlog",
    "refine": "Refine",
    "implement": "Implement",
    "review": "Review",
    "pull_request": "Pull request",
    "merge": "Merged",
}
_TERMINAL_PROJECT_STATUSES = {"done", "completed", "closed", "merged"}


def build_dashboard_state(
    state: Mapping[str, object],
    actions: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Build the dashboard contract without changing orchestration state."""

    repositories: list[dict[str, object]] = []
    all_pbis: list[dict[str, object]] = []
    for raw_repository in _mappings(state.get("repositories")):
        repository = _repository_view(raw_repository, actions)
        repositories.append(repository)
        all_pbis.extend(_mappings(repository.get("pbis")))

    readers = sum(len(_sequence(pbi.get("readers"))) for pbi in all_pbis)
    readers += sum(
        len(_mapping(pbi.get("reviewers")))
        for pbi in all_pbis
        if not _sequence(pbi.get("readers"))
    )
    subtasks = sum(len(_sequence(pbi.get("subtasks"))) for pbi in all_pbis)
    active_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "active")
    failed_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "failed")
    completed_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "completed")
    active_repositories = sum(
        1 for repository in repositories if repository.get("active") is True
    )
    active_writers = sum(
        1
        for repository in repositories
        if _mapping(repository.get("writer")).get("status") == "active"
    )

    return {
        "project": {
            "id": state.get("project_id"),
            "name": state.get("name"),
            "updated_at": state.get("updated_at"),
        },
        "project_id": state.get("project_id"),
        "name": state.get("name"),
        "updated_at": state.get("updated_at"),
        "event_limit": state.get("event_limit"),
        "counts": {
            "projects": 1,
            "repositories": len(repositories),
            "active_repositories": active_repositories,
            "pbis": len(all_pbis),
            "subtasks": subtasks,
            "writers": active_writers,
            "readers": readers,
            "active_runs": active_runs,
            "failed_runs": failed_runs,
            "completed_runs": completed_runs,
        },
        "repositories": repositories,
        "actions": [dict(action) for action in actions],
    }


def _repository_view(
    raw_repository: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    raw_writer = _mapping(raw_repository.get("writer"))
    if raw_writer:
        writer: dict[str, object] = {
            **raw_writer,
            "status": "active",
            "current_pbi": raw_writer.get("pbi_number"),
        }
    else:
        writer = {"status": "idle", "current_pbi": None}

    pbis = [
        _pbi_view(raw_pbi, raw_repository.get("name"), actions)
        for raw_pbi in _mappings(raw_repository.get("pbis"))
    ]
    return {
        "name": raw_repository.get("name"),
        "active": bool(raw_repository.get("active")),
        "writer": writer,
        "pbis": pbis,
        "counts": {
            "pbis": len(pbis),
            "subtasks": sum(len(_sequence(pbi.get("subtasks"))) for pbi in pbis),
            "readers": sum(
                len(_sequence(pbi.get("readers")))
                or len(_mapping(pbi.get("reviewers")))
                for pbi in pbis
            ),
        },
    }


def _pbi_view(
    raw_pbi: Mapping[str, object],
    repository_name: object,
    actions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    events = _mappings(raw_pbi.get("events"))
    metadata = dict(_mapping(raw_pbi.get("metadata")))
    metadata.update(_latest_metadata(events))
    raw_stage = str(raw_pbi.get("stage") or "backlog")
    raw_status = raw_pbi.get("status")
    status = str(raw_status) if raw_status is not None else "idle"
    planning_status = raw_pbi.get("planning_status")
    pull_requests = _sequence(metadata.get("pull_requests"))
    display_stage = _display_stage(raw_stage, status, planning_status, pull_requests)
    readers = _sequence(metadata.get("readers"))
    reviewers = dict(_mapping(metadata.get("reviewers")))
    if not readers and reviewers:
        readers = [
            {"id": reviewer_id, **_mapping(reviewer)}
            for reviewer_id, reviewer in reviewers.items()
        ]
    escalation = _escalation_view(metadata, events)
    subtasks = _sequence(metadata.get("subtasks"))
    matching_actions = [
        dict(action)
        for action in actions
        if _action_matches(action, repository_name, raw_pbi)
    ]
    return {
        "id": raw_pbi.get("id"),
        "number": raw_pbi.get("number"),
        "title": raw_pbi.get("title"),
        "stage": raw_stage,
        "stage_label": _display_stage_label(display_stage, planning_status),
        "stage_progress": _stage_progress(display_stage, planning_status),
        "status": status,
        "attempt": raw_pbi.get("attempt"),
        "run_id": raw_pbi.get("run_id"),
        "branch": raw_pbi.get("branch"),
        "pull_request_url": raw_pbi.get("pull_request_url"),
        "last_error": raw_pbi.get("last_error"),
        "result": raw_pbi.get("result"),
        "active": bool(raw_pbi.get("active")),
        "planning_status": planning_status,
        "claimable": bool(raw_pbi.get("claimable")),
        "subtasks": subtasks,
        "pull_requests": pull_requests,
        "readers": readers,
        "reviewers": reviewers,
        "escalation": escalation[0],
        "escalation_log": escalation[1],
        "activity": [*(_sequence(metadata.get("activity"))), *events],
        "events": events,
        "operator_actions": matching_actions,
    }


def _latest_metadata(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for event in events:
        details = _mapping(event.get("details"))
        for key in (
            "readers",
            "reviewers",
            "subtasks",
            "escalation",
            "escalation_log",
        ):
            if key in details:
                metadata[key] = details[key]
    return metadata


def _escalation_view(
    metadata: Mapping[str, object], events: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], list[object]]:
    raw_escalation = _mapping(metadata.get("escalation"))
    consecutive = _integer(raw_escalation.get("consecutive"))
    current = _integer(raw_escalation.get("current"))
    current_tier = raw_escalation.get("current_tier")
    escalation: dict[str, object] = {
        "current": current,
        "consecutive": consecutive,
    }
    if isinstance(current_tier, str) and current_tier.strip():
        escalation["current_tier"] = current_tier
    raw_log: list[object] = list(_sequence(metadata.get("escalation_log")))
    if not raw_log:
        raw_log = [
            _mapping(event.get("details"))
            for event in events
            if event.get("type") in {"escalation", "triage"}
        ]
    return escalation, raw_log


def _stage_progress(stage: str, planning_status: object = None) -> list[dict[str, str]]:
    if stage == "external":
        return [{"id": stage, "label": str(planning_status), "status": "current"}]
    current_index = _DISPLAY_STAGES.index(stage)
    return [
        {
            "id": item,
            "label": _DISPLAY_STAGE_LABELS[item],
            "status": (
                "complete"
                if index < current_index
                else "current"
                if index == current_index
                else "pending"
            ),
        }
        for index, item in enumerate(_DISPLAY_STAGES)
    ]


def _display_stage(
    stage: str,
    status: str,
    planning_status: object = None,
    pull_requests: Sequence[object] = (),
) -> str:
    normalized_planning_status = (
        planning_status.strip().lower() if isinstance(planning_status, str) else ""
    )
    if status not in {"active", "failed", "completed"}:
        if normalized_planning_status in _TERMINAL_PROJECT_STATUSES:
            merged = any(
                _mapping(pull_request).get("merged") is True
                for pull_request in pull_requests
            )
            return "merge" if merged else "external"
        if normalized_planning_status:
            project_stage = project_stage_from_status(normalized_planning_status)
            return project_stage.value if project_stage is not None else "external"
    if stage == "pull_request":
        return "pull_request" if status == "completed" else "review"
    return stage if stage in _DISPLAY_STAGES else "backlog"


def _display_stage_label(stage: str, planning_status: object) -> str:
    if stage == "external":
        return str(planning_status)
    return _DISPLAY_STAGE_LABELS[stage]


def _action_matches(
    action: Mapping[str, object],
    repository_name: object,
    raw_pbi: Mapping[str, object],
) -> bool:
    action_repository = action.get("repository")
    action_number = action.get("pbi_number")
    return action_repository == repository_name and action_number == raw_pbi.get(
        "number"
    )


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items = cast(Sequence[object], value)
    return [
        dict(cast(Mapping[str, object], item))
        for item in items
        if isinstance(item, Mapping)
    ]


def _sequence(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(cast(Sequence[object], value))


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
