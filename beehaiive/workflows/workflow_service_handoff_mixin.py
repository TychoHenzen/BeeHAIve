from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import ALLOWED_ROLE_TRANSITIONS
from .handoff_status import HandoffStatus
from .helpers import enum_role, optional_text, require_text, required_checks_pass
from .workflow_error import WorkflowError
from .workflow_role import WorkflowRole

if TYPE_CHECKING:
    from .handoff_record import HandoffRecord
    from .workspace_lease import WorkspaceLease
from typing import Any


class WorkflowServiceHandoffMixin:
    def handoff(
        self: Any,
        lease_id: str,
        source_role: WorkflowRole | str,
        target_role: WorkflowRole | str,
        commit_sha: str,
        source_state: str,
        approval_required: bool = False,
    ) -> HandoffRecord:
        lease = self._active_lease(lease_id)
        source = enum_role(source_role)
        target = enum_role(target_role)
        if target not in ALLOWED_ROLE_TRANSITIONS[source]:
            raise WorkflowError(
                "Workflow handoff from "
                f"{source.value} to {target.value} is not permitted"
            )
        rules = self._rules_for(source, target)
        normalized_commit = optional_text(commit_sha, 200)
        normalized_state = optional_text(source_state, 1_000)
        checks = self._handoff_checks(lease, normalized_state, rules, normalized_commit)
        passed = required_checks_pass(checks)
        if not passed:
            status = HandoffStatus.BLOCKED
            required_action = self._failed_action(checks)
        elif approval_required or target is WorkflowRole.OPERATOR:
            status = HandoffStatus.AWAITING_APPROVAL
            required_action = "Operator approval is required before continuation"
        else:
            status = HandoffStatus.ACCEPTED
            required_action = None
        return self.store.record_handoff(
            lease_id,
            source,
            target,
            normalized_commit,
            normalized_state,
            status,
            checks,
            rules,
            required_action,
        )

    def approve_handoff(
        self: Any, handoff_id: str, actor: str, note: str = ""
    ) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status is not HandoffStatus.AWAITING_APPROVAL:
            raise WorkflowError("Only a handoff awaiting approval can be approved")
        actor = require_text(actor, "approval actor")
        lease = self._active_lease(handoff.lease_id)
        checks = self._handoff_checks(
            lease,
            handoff.source_state,
            handoff.constitution_rules,
            handoff.commit_sha,
        )
        if not required_checks_pass(checks):
            return self.store.update_handoff(
                handoff_id,
                HandoffStatus.BLOCKED,
                self._failed_action(checks),
                checks,
                expected_status=HandoffStatus.AWAITING_APPROVAL,
            )
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.ACCEPTED,
            None,
            checks,
            actor,
            optional_text(note, 1_000) or None,
            expected_status=HandoffStatus.AWAITING_APPROVAL,
        )

    def request_clarification(
        self: Any, handoff_id: str, question: str
    ) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status in {HandoffStatus.ACCEPTED, HandoffStatus.STOPPED}:
            raise WorkflowError("This handoff cannot be paused for clarification")
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.AWAITING_CLARIFICATION,
            require_text(question, "clarification question", 1_000),
            expected_status=handoff.status,
        )

    def answer_clarification(self: Any, handoff_id: str, answer: str) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status is not HandoffStatus.AWAITING_CLARIFICATION:
            raise WorkflowError("This handoff is not awaiting clarification")
        answer = require_text(answer, "clarification answer", 1_000)
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.BLOCKED,
            f"Re-submit the handoff after clarification: {answer}",
            expected_status=HandoffStatus.AWAITING_CLARIFICATION,
        )

    def stop(self: Any, lease_id: str, reason: str) -> WorkspaceLease:
        lease = self._active_lease(lease_id)
        require_text(reason, "stop reason")
        return self.store.stop_lease(lease.lease_id, reason)

    def get_handoff(self: Any, handoff_id: str) -> HandoffRecord:
        return self._handoff(handoff_id)
