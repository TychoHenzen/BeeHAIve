from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .check_result import CheckResult
    from .handoff_status import HandoffStatus
    from .workflow_role import WorkflowRole


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """Durable handoff state, including all evidence needed to continue."""

    handoff_id: str
    lease_id: str
    source_role: WorkflowRole
    target_role: WorkflowRole
    commit_sha: str
    source_state: str
    status: HandoffStatus
    checks: tuple[CheckResult, ...]
    constitution_rules: tuple[str, ...]
    required_action: str | None
    approval_actor: str | None
    approval_note: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "lease_id": self.lease_id,
            "source_role": self.source_role.value,
            "target_role": self.target_role.value,
            "commit_sha": self.commit_sha,
            "source_state": self.source_state,
            "status": self.status.value,
            "verification_evidence": [check.as_dict() for check in self.checks],
            "constitution_rules": list(self.constitution_rules),
            "required_action": self.required_action,
            "approval_actor": self.approval_actor,
            "approval_note": self.approval_note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
