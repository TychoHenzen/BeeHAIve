from __future__ import annotations

from pathlib import Path
from typing import Any

from .gate_result import GateResult
from .handoff_status import HandoffStatus
from .helpers import optional_text, required_checks_pass
from .workflow_role import WorkflowRole


class WorkflowServiceRepairMixin:
    def before_model_call(self: Any, lease_id: str) -> GateResult:
        lease = self._active_lease(lease_id)
        checks = self.checks.run(Path(lease.worktree_path))
        latest = self.store.latest_handoff(lease_id)
        handoff_allowed = latest is None or latest.status is HandoffStatus.ACCEPTED
        failed_checks = not required_checks_pass(checks)
        required_action = None
        if not handoff_allowed and latest is not None:
            required_action = (
                latest.required_action
                if latest.required_action
                else f"Resolve handoff status: {latest.status.value}"
            )
        elif failed_checks:
            required_action = self._failed_action(checks)
        result = GateResult(
            "model_call",
            not failed_checks and handoff_allowed,
            checks,
            required_action,
        )
        self.store.record_gate(lease_id, result)
        return result

    def verify_repair(
        self: Any,
        lease_id: str,
        commit_sha: str,
        source_state: str = "conflict repair",
    ) -> GateResult:
        """Run and persist the full committed-tree evidence for a repair."""

        lease = self._active_lease(lease_id)
        rules = self.constitution.rules_for(WorkflowRole.WRITER)
        checks = self._handoff_checks(
            lease,
            optional_text(source_state, 1_000),
            rules,
            optional_text(commit_sha, 200),
        )
        passed = required_checks_pass(checks)
        result = GateResult(
            "conflict_repair",
            passed,
            tuple(checks),
            None if passed else self._failed_action(checks),
        )
        self.store.record_gate(lease_id, result)
        return result
