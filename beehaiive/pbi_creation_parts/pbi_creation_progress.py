from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

__all__ = ["PbiCreationProgress"]


@dataclass(frozen=True, slots=True)
class PbiCreationProgress:
    issue_create_started: bool = False
    issue_id: str | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    project_item_id: str | None = None
    completed_steps: tuple[str, ...] = ()
    current_step: str | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> PbiCreationProgress:
        raw_steps: object = record.get("completed_steps", [])
        steps: tuple[str, ...] = ()
        if isinstance(raw_steps, list):
            steps = tuple(
                step for step in cast(list[object], raw_steps) if isinstance(step, str)
            )
        raw_issue_id: object = record.get("issue_id")
        raw_issue_url: object = record.get("issue_url")
        raw_project_item_id: object = record.get("project_item_id")
        raw_current_step: object = record.get("current_step")
        issue_number = record.get("issue_number")
        return cls(
            issue_create_started=record.get("issue_create_started") is True,
            issue_id=raw_issue_id if isinstance(raw_issue_id, str) else None,
            issue_number=issue_number if type(issue_number) is int else None,
            issue_url=raw_issue_url if isinstance(raw_issue_url, str) else None,
            project_item_id=raw_project_item_id
            if isinstance(raw_project_item_id, str)
            else None,
            completed_steps=steps,
            current_step=raw_current_step
            if isinstance(raw_current_step, str)
            else None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "issue_create_started": self.issue_create_started,
            "issue_id": self.issue_id,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "project_item_id": self.project_item_id,
            "completed_steps": list(self.completed_steps),
            "current_step": self.current_step,
        }
