from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .helpers import require_text
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .workspace_lease import WorkspaceLease
from typing import Any


class WorkflowServiceWorkspaceMixin:
    def acquire_workspace(
        self: Any,
        agent_id: str,
        branch: str,
        worktree: str | Path,
        base_ref: str = "HEAD",
    ) -> WorkspaceLease:
        return self.worktrees.acquire(agent_id, branch, worktree, base_ref)

    def retain_workspace(
        self: Any, lease_id: str, lease_token: str | None
    ) -> WorkspaceLease:
        return self.store.retain_lease(lease_id, lease_token)

    def workspace_for_run(self: Any, run_id: str) -> WorkspaceLease | None:
        run_id = require_text(run_id, "run id")
        return self.store.get_lease_for_agent(f"dashboard-run:{run_id}")

    def cleanup_dashboard_run_workspaces(
        self: Any, run_id: str | None = None
    ) -> tuple[WorkspaceLease, ...]:
        cleaned: list[WorkspaceLease] = []
        for lease in self.store.dashboard_run_leases():
            if run_id is not None and lease.agent_id != f"dashboard-run:{run_id}":
                continue
            if lease.status is LeaseStatus.ACTIVE:
                lease = self.store.stop_lease(
                    lease.lease_id, "Dashboard worker did not survive service restart"
                )
            worktree = Path(lease.worktree_path)
            if worktree.is_dir():
                try:
                    if not self.worktrees.clean(worktree):
                        cleaned.append(lease)
                        continue
                except WorkflowError:
                    cleaned.append(lease)
                    continue
            cleaned.append(self.worktrees.release(lease.lease_id))
        return tuple(cleaned)

    def release_workspace(self: Any, lease_id: str) -> WorkspaceLease:
        return self.worktrees.release(lease_id)

    def discard_workspace(self: Any, lease_id: str, reason: str) -> WorkspaceLease:
        """Stop and remove a repair workspace without releasing dirty files."""

        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is LeaseStatus.ACTIVE:
            self.stop(lease_id, reason)
        elif lease.status is LeaseStatus.RETAINED:
            self.store.stop_lease(lease_id, reason)
        return self.worktrees.release(lease_id)
