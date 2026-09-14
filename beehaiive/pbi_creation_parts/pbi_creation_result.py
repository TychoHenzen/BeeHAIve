from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiCreationResult"]


@dataclass(frozen=True, slots=True)
class PbiCreationResult:
    issue_id: str
    issue_number: int
    issue_url: str
    labels: tuple[str, ...]
    project_item_id: str
    project_status: str
    completed_steps: tuple[str, ...]

    def as_dict(self, repository: str) -> dict[str, object]:
        return {
            "status": "complete",
            "repository": repository,
            "issue": {
                "id": self.issue_id,
                "number": self.issue_number,
                "url": self.issue_url,
            },
            "labels": list(self.labels),
            "project": {
                "item_id": self.project_item_id,
                "status": self.project_status,
            },
            "completed_steps": list(self.completed_steps),
        }
