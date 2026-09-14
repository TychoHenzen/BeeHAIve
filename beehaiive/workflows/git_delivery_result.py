from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .git_delivery_status import GitDeliveryStatus


@dataclass(frozen=True, slots=True)
class GitDeliveryResult:
    """Redacted commit and push evidence for one leased branch."""

    status: GitDeliveryStatus
    lease_id: str
    branch: str
    commit_sha: str | None
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "lease_id": self.lease_id,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "evidence": self.evidence,
        }
