from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import (
    HandoffRequest,
    RunState,
    RunStatus,
    Stage,
)
from ..storage import StoreError


class OrchestrationHandoffMixin:
    def complete_approved_handoff(
        self: Any,
        pull_request_id: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        repository, separator, number_text = pull_request_id.partition("#")
        if not separator or repository.count("/") != 1 or not number_text.isdecimal():
            raise StoreError("Pull-request id must use owner/repository#number")
        number = int(number_text)
        if number <= 0 or number > 2_147_483_647:
            raise StoreError("Pull-request number is outside the supported range")
        with self._handoff_locks.acquire(f"complete:{repository}#{number}"):
            request, pull_request_url = self.store.handoff_for_pull_request(
                repository, number
            )
            return self.provider.complete_approved_handoff(
                request,
                number,
                pull_request_url,
                expected_head,
                authorization,
            )

    def handoff(
        self: Any,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
        *,
        head_sha: str | None = None,
        verification_evidence: str = "",
    ) -> RunState:
        with self._routing_coordination(run_id), self._handoff_locks.acquire(run_id):
            return self._handoff_locked(
                run_id,
                branch,
                base_branch,
                body,
                lease_token,
                head_sha=head_sha,
                verification_evidence=verification_evidence,
            )

    def _handoff_locked(
        self: Any,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
        *,
        head_sha: str | None,
        verification_evidence: str,
    ) -> RunState:
        run = self.store.get_run(run_id)
        if run is None:
            raise StoreError(f"Unknown run: {run_id}")
        if run.status is RunStatus.COMPLETED:
            self.store.renew_lease(run_id, lease_token)
            return run
        if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
            raise StoreError("Only an active implementation run can create a handoff")
        pending = self.store.pending_handoff(run_id, lease_token)
        if pending is not None:
            requested_head = pending.head_sha if head_sha is None else head_sha
            requested_evidence = (
                verification_evidence
                if verification_evidence
                else pending.verification_evidence
            )
            if (
                pending.branch != branch
                or pending.body != body
                or (base_branch is not None and pending.base_branch != base_branch)
                or pending.head_sha != requested_head
                or pending.verification_evidence != requested_evidence
            ):
                raise StoreError("Handoff request does not match the persisted intent")
            intent = self.store.prepare_handoff(
                run_id,
                pending.branch,
                pending.base_branch,
                pending.body,
                lease_token,
                pending.head_sha,
                pending.verification_evidence,
            )
        else:
            if head_sha is not None:
                normalized_head = head_sha.strip().lower()
                if len(normalized_head) not in {40, 64} or any(
                    char not in "0123456789abcdef" for char in normalized_head
                ):
                    raise StoreError("Verified handoff head must be a Git object ID")
                if not verification_evidence.strip():
                    raise StoreError("Verified handoff evidence is required")
                head_sha = normalized_head
            resolved_base_branch = self.provider.validate_handoff(
                run.repository, branch, base_branch
            )
            intent = self.store.prepare_handoff(
                run_id,
                branch,
                resolved_base_branch,
                body,
                lease_token,
                head_sha,
                verification_evidence,
            )
        if intent.run.status is RunStatus.COMPLETED:
            return intent.run
        self._ensure_handoff_routing_allowed(run_id)
        self.store.validate_lease(run_id, lease_token)
        result = self.provider.create_handoff(
            HandoffRequest(
                project_id=intent.run.project_id,
                repository=intent.run.repository,
                pbi_number=intent.run.pbi_number,
                title=intent.run.title,
                branch=intent.branch,
                base_branch=intent.base_branch,
                body=intent.body,
                run_id=intent.run.run_id,
                head_sha=intent.head_sha,
                verification_evidence=intent.verification_evidence,
                mutation_audit=self.store,
            )
        )
        self._complete_routing_problem(run_id)
        completed = self.store.record_handoff(
            run_id,
            result.branch,
            result.pull_request_url,
            result.pull_request_number,
            lease_token,
            result=(
                f"{intent.body}\n\nPushed head: {intent.head_sha}\n\n"
                f"Verification evidence:\n{intent.verification_evidence}"
                if intent.head_sha is not None
                else intent.body
            ),
        )
        return completed

    def _ensure_handoff_routing_allowed(self: Any, run_id: str) -> None:
        if self.model_router is None:
            return
        self._ensure_routing_problem(run_id)
        reason = self.model_router.handoff_limit_reason(run_id)
        if reason is not None:
            raise StoreError(f"Routing handoff requires human action: {reason}")
