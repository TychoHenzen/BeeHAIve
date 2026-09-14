from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = ["PbiRefinementUpdateResult"]


@dataclass(frozen=True, slots=True)
class PbiRefinementUpdateResult:
    status: Literal["complete", "partial"]
    issue_number: int
    issue_url: str
    labels: tuple[str, ...]
    project_item_id: str
    project_status: str
    linked_sub_issues: tuple[Mapping[str, object], ...]
    completed_steps: tuple[str, ...]
    pending_step: str | None = None
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "issue": {"number": self.issue_number, "url": self.issue_url},
            "labels": list(self.labels),
            "project": {
                "item_id": self.project_item_id,
                "status": self.project_status,
            },
            "linked_sub_issues": [dict(issue) for issue in self.linked_sub_issues],
            "completed_steps": list(self.completed_steps),
            "pending_step": self.pending_step,
            "failure_code": self.failure_code,
        }
