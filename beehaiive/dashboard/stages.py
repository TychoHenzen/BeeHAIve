from __future__ import annotations

from collections.abc import Sequence

from ..models import PROJECT_TERMINAL_STATUSES, project_stage_from_status
from .constants import DISPLAY_STAGE_LABELS, DISPLAY_STAGES
from .values import mapping


def stage_progress(stage: str, planning_status: object = None) -> list[dict[str, str]]:
    if stage == "external":
        return [{"id": stage, "label": str(planning_status), "status": "current"}]
    current_index = DISPLAY_STAGES.index(stage)
    return [
        {
            "id": item,
            "label": DISPLAY_STAGE_LABELS[item],
            "status": (
                "complete"
                if index < current_index
                else "current"
                if index == current_index
                else "pending"
            ),
        }
        for index, item in enumerate(DISPLAY_STAGES)
    ]


def display_stage(
    stage: str,
    status: str,
    planning_status: object = None,
    pull_requests: Sequence[object] = (),
) -> str:
    normalized_planning_status = (
        planning_status.strip().lower() if isinstance(planning_status, str) else ""
    )
    if status not in {"active", "awaiting_operator", "failed", "completed"}:
        if normalized_planning_status in PROJECT_TERMINAL_STATUSES:
            merged = any(
                mapping(pull_request).get("merged") is True
                for pull_request in pull_requests
            )
            return "merge" if merged else "external"
        if normalized_planning_status:
            project_stage = project_stage_from_status(normalized_planning_status)
            return project_stage.value if project_stage is not None else "external"
    if stage == "pull_request":
        return "pull_request" if status == "completed" else "review"
    return stage if stage in DISPLAY_STAGES else "backlog"


def display_stage_label(stage: str, planning_status: object) -> str:
    if stage == "external":
        return str(planning_status)
    return DISPLAY_STAGE_LABELS[stage]
