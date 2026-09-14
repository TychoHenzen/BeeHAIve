from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MergeHandoff:
    pull_request_id: str
    cycle_id: str
    head_sha: str
    approved_by_human: bool
    approval_actor: str | None = None
    approval_reason: str | None = None
    approval_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "pull_request_id": self.pull_request_id,
            "cycle_id": self.cycle_id,
            "head_sha": self.head_sha,
            "approved_by_human": self.approved_by_human,
            "approval_actor": self.approval_actor,
            "approval_reason": self.approval_reason,
            "approval_at": self.approval_at,
            "status": "merge_handoff",
        }
