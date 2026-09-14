from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .helpers import json_object

if TYPE_CHECKING:
    from .review_cycle_status import ReviewCycleStatus


@dataclass(frozen=True, slots=True)
class ReviewCycle:
    cycle_id: str
    pull_request_id: str
    head_sha: str
    cycle_number: int
    status: ReviewCycleStatus
    human_approval: bool
    required_action: str | None
    created_at: str
    updated_at: str
    approval_actor: str | None = None
    approval_reason: str | None = None
    approval_at: str | None = None
    github_evidence_json: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "cycle_id": self.cycle_id,
            "pull_request_id": self.pull_request_id,
            "head_sha": self.head_sha,
            "cycle_number": self.cycle_number,
            "status": self.status.value,
            "human_approval": self.human_approval,
            "required_action": self.required_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval_actor": self.approval_actor,
            "approval_reason": self.approval_reason,
            "approval_at": self.approval_at,
        }
        if self.github_evidence_json is not None:
            result["github_evidence"] = json_object(self.github_evidence_json)
        return result
