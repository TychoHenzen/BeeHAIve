from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .review_repair_transition_status import ReviewRepairTransitionStatus

if TYPE_CHECKING:
    from .review_repair_status import ReviewRepairStatus


@dataclass(frozen=True, slots=True)
class ReviewRepairAttempt:
    attempt_id: str
    cycle_id: str
    pull_request_id: str
    head_sha: str
    finding_ids: tuple[str, ...]
    actor: str
    status: ReviewRepairStatus
    created_at: str
    updated_at: str
    lease_id: str | None = None
    commit_sha: str | None = None
    push_evidence: dict[str, object] | None = None
    result: str | None = None
    required_action: str | None = None
    cancellation_requested: bool = False
    review_transition_status: ReviewRepairTransitionStatus = (
        ReviewRepairTransitionStatus.NOT_REQUIRED
    )
    review_transition_cycle_id: str | None = None
    review_transition_required_action: str | None = None

    def review_transition_as_dict(self) -> dict[str, object]:
        return {
            "status": self.review_transition_status.value,
            "cycle_id": self.review_transition_cycle_id,
            "required_action": self.review_transition_required_action,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "cycle_id": self.cycle_id,
            "pull_request_id": self.pull_request_id,
            "head_sha": self.head_sha,
            "finding_ids": list(self.finding_ids),
            "actor": self.actor,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lease_id": self.lease_id,
            "commit_sha": self.commit_sha,
            "push_evidence": self.push_evidence,
            "result": self.result,
            "required_action": self.required_action,
            "cancellation_requested": self.cancellation_requested,
            "review_transition": self.review_transition_as_dict(),
        }
