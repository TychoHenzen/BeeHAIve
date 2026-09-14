from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from typing import Any

from ..provider import ProviderError
from ..review import (
    ReviewError,
    ReviewRepairAttempt,
    ReviewRepairStatus,
)
from ..routing import (
    AttemptOutcome,
    RoutingError,
)
from ..workflow import (
    WorkflowError,
    WorkspaceLease,
)
from .repair_cancelled import RepairCancelled
from .routed_repair_agent import RoutedRepairAgent


class ReviewRepairRunMixin:
    def _run(self: Any, attempt_id: str, cancelled: Event) -> None:
        status = ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        required_action: str | None = "Review repair worker did not complete"
        result: str | None = None
        commit_sha: str | None = None
        push_evidence: dict[str, object] | None = None
        lease: WorkspaceLease | None = None
        heartbeat_stop = Event()
        heartbeat_errors: list[Exception] = []
        heartbeat: Thread | None = None
        claimed = False
        finished_attempt: ReviewRepairAttempt | None = None
        source_ref = f"refs/beehaiive/review-repair/{attempt_id}/source"
        try:
            attempt = self.reviews.repair_attempt(attempt_id)
            if not self.reviews.store.claim_repair_attempt(attempt_id):
                return
            claimed = True
            attempt = self.reviews.repair_attempt(attempt_id)
            self._raise_if_cancelled(attempt, cancelled)
            repository, number = self.pull_request_parts(attempt.pull_request_id)
            review_target = self.review_provider.get_pull_request(
                attempt.pull_request_id
            )
            if (
                review_target.pull_request_id != attempt.pull_request_id
                or review_target.head_sha != attempt.head_sha
                or not review_target.ready
            ):
                raise ProviderError("Review pull-request identity or head changed")
            before = self.provider.get_pull_request(repository, number)
            self._validate_snapshot(before, attempt, repository, number)

            self.worktree_root.mkdir(parents=True, exist_ok=True)
            self.workflow.worktrees.fetch_exact_branch(
                before.source_branch or "", attempt.head_sha, source_ref
            )
            lease = self.workflow.acquire_workspace(
                f"review-repair:{attempt_id}",
                f"codex/review-repair-{attempt_id[:12]}",
                self.worktree_root / attempt_id,
                source_ref,
            )
            if lease is None:
                raise WorkflowError("Review repair workspace lease is unavailable")
            if not self.reviews.store.attach_repair_lease(attempt_id, lease.lease_id):
                self._raise_if_cancelled(
                    self.reviews.repair_attempt(attempt_id), cancelled
                )
                raise WorkflowError("Repair attempt could not claim its workspace")

            def renew_lease() -> None:
                while not heartbeat_stop.wait(
                    self.workflow.store.lease_heartbeat_seconds
                ):
                    try:
                        self.workflow.store.renew_lease(
                            lease.lease_id, lease.lease_token
                        )
                    except Exception as exc:
                        heartbeat_errors.append(exc)
                        cancelled.set()
                        self.agent.cancel(attempt_id)
                        return

            heartbeat = Thread(
                target=renew_lease,
                name=f"beehaiive-review-lease-{attempt_id[:8]}",
                daemon=True,
            )
            heartbeat.start()
            self._raise_if_cancelled(self.reviews.repair_attempt(attempt_id), cancelled)
            prompt = self._prompt(attempt)
            self.router.begin(attempt_id)
            routed = self.router.execute(
                attempt_id,
                RoutedRepairAgent(
                    self.agent,
                    attempt_id,
                    Path(lease.worktree_path),
                    prompt,
                    cancelled,
                ),
            )
            result = self._safe_text(routed.execution_result or "", 4_000) or None
            if heartbeat_errors:
                raise WorkflowError("Review repair workspace lease was lost")
            if (
                cancelled.is_set()
                or self.reviews.repair_attempt(attempt_id).cancellation_requested
            ):
                status = ReviewRepairStatus.CANCELLED
                required_action = None
                return
            if (
                routed.attempt is None
                or routed.attempt.outcome is not AttemptOutcome.SUCCESS
            ):
                status = (
                    ReviewRepairStatus.HUMAN_ACTION_REQUIRED
                    if routed.state.required_action
                    else ReviewRepairStatus.FAILED
                )
                required_action = self._safe_text(
                    routed.state.required_action
                    or (
                        routed.attempt.failure_context
                        if routed.attempt is not None
                        else ""
                    )
                    or "Selected finding repair failed",
                    1_000,
                )
                return

            commit_sha = self.workflow.worktrees.head(lease.worktree_path)
            if commit_sha == attempt.head_sha:
                raise WorkflowError("Repair worker did not create a commit")
            if not self.workflow.worktrees.contains_commit(
                lease.worktree_path, attempt.head_sha
            ):
                raise WorkflowError("Repair commit does not preserve source history")
            if not self.workflow.worktrees.clean(lease.worktree_path):
                raise WorkflowError("Repair worktree has uncommitted changes")
            gate = self.workflow.verify_repair(
                lease.lease_id, commit_sha, "selected review finding repair"
            )
            if not gate.allowed:
                raise WorkflowError(
                    gate.required_action or "Repair verification gates failed"
                )
            self._validate_active_lease(lease)
            current = self.provider.get_pull_request(repository, number)
            if not self._same_source_identity(before, current):
                raise ProviderError("Pull-request source identity changed before push")
            self._validate_push_remote(repository, lease.worktree_path)
            push_evidence = {
                "repository": repository,
                "pull_request_number": number,
                "source_branch": before.source_branch or "",
                "expected_head": attempt.head_sha,
                "pushed_head": commit_sha,
            }
            if not self.reviews.store.begin_repair_push(
                attempt_id, lease.lease_id, commit_sha, push_evidence
            ):
                self._raise_if_cancelled(
                    self.reviews.repair_attempt(attempt_id), cancelled
                )
                raise WorkflowError("Repair attempt could not claim its source push")
            self._validate_active_lease(lease)
            if heartbeat_errors:
                raise WorkflowError("Review repair workspace lease was lost")
            self._raise_if_cancelled(self.reviews.repair_attempt(attempt_id), cancelled)
            self.provider.update_source_branch(
                current, lease.worktree_path, attempt.head_sha, commit_sha
            )
            confirmed = self.provider.get_pull_request(repository, number)
            confirmed_review = self.review_provider.get_pull_request(
                attempt.pull_request_id
            )
            if (
                not self._same_source_identity(
                    before, confirmed, expected_source_head=commit_sha
                )
                or confirmed.source_head != commit_sha
                or confirmed_review.head_sha != commit_sha
            ):
                raise ProviderError(
                    "Pull-request readback did not confirm the pushed head"
                )
            if heartbeat_errors:
                raise WorkflowError("Review repair workspace lease was lost")
            status = ReviewRepairStatus.SUCCEEDED
            required_action = None
        except RepairCancelled:
            status = ReviewRepairStatus.CANCELLED
            required_action = None
        except (ProviderError, WorkflowError, ReviewError, RoutingError) as exc:
            required_action = self._safe_text(str(exc), 1_000)
        except Exception as exc:
            required_action = self._safe_text(
                f"Unexpected repair worker failure: {exc}", 1_000
            )
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
            if heartbeat_errors and status is not ReviewRepairStatus.SUCCEEDED:
                required_action = "Review repair workspace lease was lost"
            if lease is not None:
                try:
                    self.workflow.discard_workspace(
                        lease.lease_id, "Selected review repair finished"
                    )
                except Exception as exc:
                    status = ReviewRepairStatus.HUMAN_ACTION_REQUIRED
                    required_action = self._safe_text(
                        f"Repair workspace cleanup failed: {exc}", 1_000
                    )
            if claimed:
                self.workflow.worktrees.remove_ref(source_ref)
                finished_attempt = self.reviews.store.finish_repair_attempt(
                    attempt_id,
                    status,
                    commit_sha=commit_sha,
                    push_evidence=push_evidence,
                    result=result,
                    required_action=required_action,
                )
            with self._lock:
                self._threads.pop(attempt_id, None)
                self._cancel_events.pop(attempt_id, None)
            if (
                finished_attempt is not None
                and finished_attempt.status is ReviewRepairStatus.SUCCEEDED
            ):
                self._start_review_transition(finished_attempt)
