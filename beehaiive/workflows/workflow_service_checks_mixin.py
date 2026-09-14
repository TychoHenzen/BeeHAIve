from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .check_result import CheckResult
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .handoff_record import HandoffRecord
    from .workflow_role import WorkflowRole
    from .workspace_lease import WorkspaceLease
from typing import Any


class WorkflowServiceChecksMixin:
    def _active_lease(self: Any, lease_id: str) -> WorkspaceLease:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is not LeaseStatus.ACTIVE:
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        return lease

    def _handoff(self: Any, handoff_id: str) -> HandoffRecord:
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError(f"Unknown handoff: {handoff_id}")
        return handoff

    def _rules_for(
        self: Any, source: WorkflowRole, target: WorkflowRole
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.constitution.rules_for(source),
                    *self.constitution.rules_for(target),
                )
            )
        )

    def _handoff_checks(
        self: Any,
        lease: WorkspaceLease,
        source_state: str,
        rules: Sequence[str],
        commit_sha: str,
    ) -> list[CheckResult]:
        checks = list(self.checks.run(Path(lease.worktree_path)))
        if not source_state:
            checks.append(
                CheckResult("source_state", False, "Source state is required")
            )
        else:
            checks.append(CheckResult("source_state", True, source_state))
        checks.append(
            CheckResult(
                "constitution",
                bool(rules),
                f"Loaded {len(rules)} applicable constitution rules",
            )
        )
        if not commit_sha:
            checks.append(CheckResult("commit", False, "A committed SHA is required"))
        else:
            checks.append(self._commit_check(Path(lease.worktree_path), commit_sha))
        checks.append(self._clean_check(Path(lease.worktree_path)))
        return checks

    def _commit_check(self: Any, workspace: Path, expected: str) -> CheckResult:
        try:
            actual = self.worktrees.head(workspace)
        except WorkflowError as exc:
            return CheckResult("commit", False, str(exc))
        return CheckResult(
            "commit",
            actual == expected,
            f"HEAD {actual}; expected {expected}",
        )

    def _clean_check(self: Any, workspace: Path) -> CheckResult:
        try:
            clean = self.worktrees.clean(workspace)
        except WorkflowError as exc:
            return CheckResult("working_tree", False, str(exc))
        return CheckResult(
            "working_tree",
            clean,
            "Working tree is clean"
            if clean
            else "Working tree has uncommitted changes",
        )

    @staticmethod
    def _failed_action(checks: Sequence[CheckResult]) -> str:
        failed = ", ".join(
            check.name for check in checks if check.required and not check.passed
        )
        return f"Resolve failed or missing evidence: {failed}"
