from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .check_result import CheckResult
    from .repair_status import RepairStatus


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Persisted conflict-repair identity, evidence, and operator state."""

    repair_id: str
    pull_request_id: str
    repository: str
    pull_request_number: int
    source_branch: str
    target_branch: str
    expected_head: str
    target_head: str
    repair_branch: str
    worktree_path: str
    lease_id: str | None
    status: RepairStatus
    repaired_head: str | None
    checks: tuple[CheckResult, ...]
    evidence: Mapping[str, object]
    required_action: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "repair_id": self.repair_id,
            "pull_request_id": self.pull_request_id,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "expected_head": self.expected_head,
            "target_head": self.target_head,
            "repair_branch": self.repair_branch,
            "worktree_path": self.worktree_path,
            "lease_id": self.lease_id,
            "status": self.status.value,
            "repaired_head": self.repaired_head,
            "verification_evidence": [check.as_dict() for check in self.checks],
            "evidence": dict(self.evidence),
            "required_action": self.required_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
