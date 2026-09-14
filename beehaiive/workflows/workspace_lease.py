from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lease_status import LeaseStatus


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """A unique active agent, branch, and worktree combination."""

    lease_id: str
    agent_id: str
    branch: str
    worktree_path: str
    status: LeaseStatus
    created_at: str
    updated_at: str
    lease_token: str | None = None
    expires_at: str | None = None
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lease_token": self.lease_token,
            "expires_at": self.expires_at,
            "stop_reason": self.stop_reason,
        }
