"""Fail-closed conflict repair for an existing pull request."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .agent import redact_worker_text
from .models import PullRequestSnapshot
from .provider import ProjectProvider, ProviderError
from .routing import AttemptOutcome, ModelExecution
from .workflow import (
    CheckResult,
    RepairRecord,
    RepairStatus,
    WorkflowError,
    WorkflowService,
)


class ConflictRepairAgent(Protocol):
    """Bounded write-enabled agent used only inside a leased worktree."""

    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution: ...


class ConflictRepairService:
    """Coordinate one idempotent repair event without merging or closing a PR."""

    def __init__(
        self,
        workflow: WorkflowService,
        provider: ProjectProvider,
        agent: ConflictRepairAgent,
        worktree_root: str | Path | None = None,
    ) -> None:
        self.workflow = workflow
        self.provider = provider
        self.agent = agent
        self.worktree_root = (
            Path(worktree_root).resolve()
            if worktree_root is not None
            else workflow.worktrees.repository / ".beehaiive" / "repair-worktrees"
        )

    def repair(self, repository: str, pull_request_number: int) -> RepairRecord:
        """Repair a proven conflict or persist the operator state that blocks it."""

        if pull_request_number <= 0:
            raise WorkflowError("Pull-request number must be positive")
        pull_request_id = f"{repository}#{pull_request_number}"
        try:
            snapshot = self.provider.get_pull_request(repository, pull_request_number)
        except Exception as exc:
            record = self._begin(
                pull_request_id,
                repository,
                pull_request_number,
                source_branch="",
                target_branch="",
                expected_head="",
                target_head="",
                evidence={"provider_error": self._error(exc)},
            )
            if record.status is not RepairStatus.RUNNING:
                return record
            return self.workflow.store.finish_repair(
                record.repair_id,
                RepairStatus.AWAITING_CLARIFICATION,
                "Provider evidence is unavailable or malformed",
                dict(record.evidence),
            )

        record = self._begin(
            snapshot.pull_request_id,
            repository,
            pull_request_number,
            source_branch=snapshot.source_branch or "",
            target_branch=snapshot.target_branch or "",
            expected_head=snapshot.source_head or "",
            target_head=snapshot.target_head or "",
            evidence={"before": snapshot.as_dict()},
        )
        if record.status is not RepairStatus.RUNNING:
            return record
        if snapshot.conflict_state == "clean":
            return self.workflow.store.finish_repair(
                record.repair_id,
                RepairStatus.NOT_REQUIRED,
                "Pull request is not conflicting",
                dict(record.evidence),
            )
        if snapshot.conflict_state != "conflicting":
            return self.workflow.store.finish_repair(
                record.repair_id,
                RepairStatus.AWAITING_CLARIFICATION,
                "Pull-request conflict evidence is unknown or contradictory",
                dict(record.evidence),
            )

        return self._run_repair(record, snapshot)

    def _begin(
        self,
        pull_request_id: str,
        repository: str,
        pull_request_number: int,
        *,
        source_branch: str,
        target_branch: str,
        expected_head: str,
        target_head: str,
        evidence: Mapping[str, object],
    ) -> RepairRecord:
        repair_id = str(uuid4())
        return self.workflow.store.begin_repair(
            repair_id,
            pull_request_id,
            repository,
            pull_request_number,
            source_branch,
            target_branch,
            expected_head,
            target_head,
            f"codex/repair-{repair_id[:12]}",
            str(self.worktree_root / repair_id),
            evidence,
        )

    def _run_repair(
        self, record: RepairRecord, before: PullRequestSnapshot
    ) -> RepairRecord:
        assert before.source_branch is not None
        assert before.source_head is not None
        assert before.target_branch is not None
        assert before.target_head is not None
        source_ref = f"refs/beehaiive/repair/{record.repair_id}/source"
        target_ref = f"refs/beehaiive/repair/{record.repair_id}/target"
        lease_id: str | None = None
        checks: tuple[CheckResult, ...] = ()
        status = RepairStatus.BLOCKED
        required_action = "Repair did not complete"
        evidence = dict(record.evidence)
        repaired_head: str | None = None
        try:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            self.workflow.worktrees.fetch_exact_branch(
                before.source_branch, before.source_head, source_ref
            )
            self.workflow.worktrees.fetch_exact_branch(
                before.target_branch, before.target_head, target_ref
            )
            lease = self.workflow.acquire_workspace(
                f"conflict-repair:{record.repair_id}",
                record.repair_branch,
                record.worktree_path,
                before.source_head,
            )
            lease_id = lease.lease_id
            self.workflow.store.attach_repair_workspace(
                record.repair_id, lease.lease_id, lease.worktree_path
            )
            merge = self.workflow.worktrees.integrate_target(
                lease.worktree_path, before.source_head, before.target_head
            )
            evidence["integration"] = {
                "conflicted": merge.conflicted,
                "evidence": merge.evidence,
            }
            execution = self.agent.execute_repair(
                record.repair_id,
                Path(lease.worktree_path),
                before.source_branch,
                before.target_branch,
            )
            evidence["agent"] = self._execution_evidence(execution)
            if execution.outcome is not AttemptOutcome.SUCCESS:
                required_action = execution.failure_context or "Repair agent failed"
                status = RepairStatus.BLOCKED
                return self._finish_after_cleanup(
                    record,
                    status,
                    required_action,
                    evidence,
                    checks,
                    repaired_head,
                    lease_id,
                    source_ref,
                    target_ref,
                )
            repaired_head = self.workflow.worktrees.head(lease.worktree_path)
            if not self.workflow.worktrees.contains_commit(
                lease.worktree_path, before.target_head
            ):
                raise WorkflowError("Repair commit does not include the target head")
            gate = self.workflow.verify_repair(lease.lease_id, repaired_head)
            checks = gate.checks
            evidence["gates"] = gate.as_dict()
            if not gate.allowed:
                required_action = gate.required_action or "Repair gates failed"
                status = RepairStatus.BLOCKED
                return self._finish_after_cleanup(
                    record,
                    status,
                    required_action,
                    evidence,
                    checks,
                    repaired_head,
                    lease_id,
                    source_ref,
                    target_ref,
                )
            current = self.provider.get_pull_request(before.repository, before.number)
            evidence["before_update"] = current.as_dict()
            if not self._same_repair_identity(before, current):
                raise ProviderError(
                    "Pull-request identity changed before branch update"
                )
            self.provider.update_source_branch(
                current,
                lease.worktree_path,
                before.source_head,
                repaired_head,
            )
            confirmed = self.provider.get_pull_request(before.repository, before.number)
            evidence["after_update"] = confirmed.as_dict()
            if (
                confirmed.pull_request_id != before.pull_request_id
                or confirmed.source_head != repaired_head
                or confirmed.conflict_state != "clean"
            ):
                raise ProviderError(
                    "Pull-request readback did not confirm the repaired clean head"
                )
            status = RepairStatus.SUCCEEDED
            required_action = None
        except (ProviderError, WorkflowError) as exc:
            required_action = self._error(exc)
            status = RepairStatus.AWAITING_CLARIFICATION
        except Exception as exc:  # pragma: no cover - defensive operator boundary
            required_action = self._error(exc)
            status = RepairStatus.AWAITING_CLARIFICATION
        return self._finish_after_cleanup(
            record,
            status,
            required_action,
            evidence,
            checks,
            repaired_head,
            lease_id,
            source_ref,
            target_ref,
        )

    def _finish_after_cleanup(
        self,
        record: RepairRecord,
        status: RepairStatus,
        required_action: str | None,
        evidence: dict[str, object],
        checks: tuple[CheckResult, ...],
        repaired_head: str | None,
        lease_id: str | None,
        source_ref: str,
        target_ref: str,
    ) -> RepairRecord:
        try:
            if lease_id is not None:
                self.workflow.discard_workspace(lease_id, "Conflict repair finished")
        except Exception as exc:
            status = RepairStatus.AWAITING_CLARIFICATION
            required_action = f"Repair workspace cleanup failed: {self._error(exc)}"
        finally:
            self.workflow.worktrees.remove_ref(source_ref)
            self.workflow.worktrees.remove_ref(target_ref)
        return self.workflow.store.finish_repair(
            record.repair_id,
            status,
            required_action,
            evidence,
            checks,
            repaired_head,
        )

    @staticmethod
    def _same_repair_identity(
        expected: PullRequestSnapshot, current: PullRequestSnapshot
    ) -> bool:
        return (
            expected.pull_request_id == current.pull_request_id
            and expected.repository == current.repository
            and expected.source_branch == current.source_branch
            and expected.source_head == current.source_head
            and expected.target_branch == current.target_branch
            and expected.target_head == current.target_head
            and current.conflict_state == "conflicting"
        )

    @staticmethod
    def _execution_evidence(execution: ModelExecution) -> dict[str, object]:
        return {
            "outcome": str(execution.outcome),
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "result": redact_worker_text(execution.result),
            "failure_context": redact_worker_text(execution.failure_context),
        }

    @staticmethod
    def _error(error: Exception) -> str:
        message = redact_worker_text(str(error), ())
        return message or type(error).__name__
