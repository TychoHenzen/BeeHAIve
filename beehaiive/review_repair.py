"""Dispatch one isolated repair for an operator-selected review finding set."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol

from .agent import redact_worker_text
from .models import PullRequestSnapshot
from .provider import ProjectProvider, ProviderError
from .review import (
    FindingStatus,
    PullRequestReviewProvider,
    ReviewError,
    ReviewRepairAttempt,
    ReviewRepairStatus,
    ReviewRepairTransitionStatus,
    ReviewService,
)
from .routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelSpec,
    RoutingDecision,
    RoutingError,
)
from .workflow import (
    LeaseStatus,
    WorkflowError,
    WorkflowService,
    WorkspaceLease,
    repository_identity,
)

MAX_REPAIR_PROMPT_CHARS = 24_000


class SelectedRepairAgent(Protocol):
    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution: ...

    def cancel(self, problem_id: str) -> None: ...


class ReviewRepairService:
    """Persist one cycle-bound selection and run it in a leased worktree."""

    def __init__(
        self,
        reviews: ReviewService,
        workflow: WorkflowService,
        provider: ProjectProvider,
        router: ModelRouter,
        agent: SelectedRepairAgent,
        worktree_root: str | Path | None = None,
    ) -> None:
        if reviews.provider is None:
            raise ValueError("A pull-request review provider is required")
        self.reviews = reviews
        self.review_provider: PullRequestReviewProvider = reviews.provider
        self.workflow = workflow
        self.provider = provider
        self.router = router
        self.agent = agent
        self.worktree_root = (
            Path(worktree_root).resolve()
            if worktree_root is not None
            else workflow.worktrees.repository
            / ".beehaiive"
            / "review-repair-worktrees"
        )
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._cancel_events: dict[str, Event] = {}

    def dispatch(
        self, cycle_id: str, finding_ids: tuple[str, ...], actor: str
    ) -> ReviewRepairAttempt:
        attempt, created = self.reviews.create_repair_attempt(
            cycle_id, finding_ids, actor
        )
        if not created and attempt.status is not ReviewRepairStatus.QUEUED:
            self._recover_attempt(attempt)
            return self.reviews.repair_attempt(attempt.attempt_id)
        self._start_worker(attempt.attempt_id)
        return self.reviews.repair_attempt(attempt.attempt_id)

    def _start_worker(self, attempt_id: str) -> None:
        cancelled = Event()
        thread = Thread(
            target=self._run,
            args=(attempt_id, cancelled),
            name=f"beehaiive-review-repair-{attempt_id[:8]}",
            daemon=True,
        )
        with self._lock:
            if attempt_id in self._threads:
                return
            self._threads[attempt_id] = thread
            self._cancel_events[attempt_id] = cancelled
        try:
            thread.start()
        except RuntimeError as exc:
            self.reviews.store.finish_repair_attempt(
                attempt_id,
                ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
                required_action=self._safe_text(str(exc), 1_000),
            )
            with self._lock:
                self._threads.pop(attempt_id, None)
                self._cancel_events.pop(attempt_id, None)

    def recover(self) -> None:
        """Resume queued attempts and reconcile attempts left by a stopped worker."""

        for attempt in self.reviews.store.pending_repair_attempts():
            self._recover_attempt(attempt)

    def get(self, attempt_id: str) -> ReviewRepairAttempt:
        self._recover_attempt(self.reviews.repair_attempt(attempt_id))
        return self.reviews.repair_attempt(attempt_id)

    def cancel(self, attempt_id: str, actor: str) -> ReviewRepairAttempt:
        attempt = self.reviews.cancel_repair_attempt(attempt_id, actor)
        if (
            attempt.status is ReviewRepairStatus.RUNNING
            and attempt.cancellation_requested
        ):
            with self._lock:
                cancelled = self._cancel_events.get(attempt_id)
            if cancelled is not None:
                cancelled.set()
            self.agent.cancel(attempt_id)
        return self.reviews.repair_attempt(attempt_id)

    def wait(
        self, attempt_id: str, timeout: float | None = None
    ) -> ReviewRepairAttempt:
        """Wait for one local worker, primarily for deterministic tests."""

        with self._lock:
            thread = self._threads.get(attempt_id)
        if thread is not None:
            thread.join(timeout)
        return self.reviews.repair_attempt(attempt_id)

    def _recover_attempt(self, attempt: ReviewRepairAttempt) -> None:
        if attempt.status is ReviewRepairStatus.QUEUED:
            self._start_worker(attempt.attempt_id)
            return
        if (
            attempt.status is ReviewRepairStatus.SUCCEEDED
            and attempt.review_transition_status
            in {
                ReviewRepairTransitionStatus.PENDING,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
            }
        ):
            self._start_review_transition(attempt)
            return
        if attempt.status not in {
            ReviewRepairStatus.RUNNING,
            ReviewRepairStatus.PUSHING,
        }:
            return
        with self._lock:
            if attempt.attempt_id in self._threads:
                return
        if attempt.lease_id is None:
            if self._within_worker_grace(attempt):
                return
        else:
            lease = self.workflow.store.get_lease(attempt.lease_id)
            if lease is not None and lease.status is LeaseStatus.ACTIVE:
                return
            if lease is not None and self._within_worker_grace(attempt):
                return
        if attempt.status is ReviewRepairStatus.PUSHING:
            self._recover_push(attempt)
            return
        self.reviews.store.finish_repair_attempt(
            attempt.attempt_id,
            ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
            required_action=(
                "The repair worker stopped before completion. Verify the pull-request "
                "head, then start a new review cycle before retrying."
            ),
        )

    def _within_worker_grace(self, attempt: ReviewRepairAttempt) -> bool:
        grace_seconds = max(60, self.workflow.store.lease_heartbeat_seconds * 3)
        try:
            updated_at = datetime.fromisoformat(attempt.updated_at)
            return (datetime.now(UTC) - updated_at).total_seconds() < grace_seconds
        except ValueError:
            return False

    def _recover_push(self, attempt: ReviewRepairAttempt) -> None:
        evidence = attempt.push_evidence
        identity = self._push_evidence_identity(attempt)
        confirmed = False
        detail: str | None = None
        if identity is not None:
            repository, number, branch = identity
            try:
                current = self.provider.get_pull_request(repository, number)
                review_target = self.review_provider.get_pull_request(
                    attempt.pull_request_id
                )
                confirmed = (
                    current.repository.casefold() == repository.casefold()
                    and current.number == number
                    and current.source_branch == branch
                    and current.source_head == attempt.commit_sha
                    and current.state == "OPEN"
                    and not current.merged
                    and review_target.pull_request_id == attempt.pull_request_id
                    and review_target.head_sha == attempt.commit_sha
                    and review_target.ready
                )
            except Exception as exc:
                detail = self._safe_text(str(exc), 500)
        if confirmed:
            completed = self.reviews.store.finish_repair_attempt(
                attempt.attempt_id,
                ReviewRepairStatus.SUCCEEDED,
                commit_sha=attempt.commit_sha,
                push_evidence=evidence,
                result="Push confirmed during restart recovery",
            )
            self._recover_attempt(completed)
            return
        required_action = (
            "The push outcome could not be confirmed after the worker stopped. "
            "Verify the pull-request head before retrying."
        )
        if detail:
            required_action = f"{required_action} Provider readback failed: {detail}"
        self.reviews.store.finish_repair_attempt(
            attempt.attempt_id,
            ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
            commit_sha=attempt.commit_sha,
            push_evidence=evidence,
            required_action=required_action,
        )

    def _run(self, attempt_id: str, cancelled: Event) -> None:
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
            repository, number = self._pull_request_parts(attempt.pull_request_id)
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
                _RoutedRepairAgent(
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
        except _RepairCancelled:
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

    @staticmethod
    def _push_evidence_identity(
        attempt: ReviewRepairAttempt,
    ) -> tuple[str, int, str] | None:
        evidence = attempt.push_evidence
        if (
            evidence is None
            or attempt.commit_sha is None
            or attempt.commit_sha == attempt.head_sha
            or evidence.get("expected_head") != attempt.head_sha
            or evidence.get("pushed_head") != attempt.commit_sha
        ):
            return None
        repository = evidence.get("repository")
        number = evidence.get("pull_request_number")
        branch = evidence.get("source_branch")
        if (
            not isinstance(repository, str)
            or not repository
            or type(number) is not int
            or number <= 0
            or not isinstance(branch, str)
            or not branch
        ):
            return None
        try:
            expected_repository, expected_number = (
                ReviewRepairService._pull_request_parts(attempt.pull_request_id)
            )
        except ReviewError:
            return None
        if (
            repository.casefold() != expected_repository.casefold()
            or number != expected_number
        ):
            return None
        return repository, number, branch

    def _start_review_transition(self, attempt: ReviewRepairAttempt) -> None:
        identity = self._push_evidence_identity(attempt)
        if identity is None:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED,
                "Verify the repair commit and pushed pull-request evidence before "
                "starting a fresh review cycle.",
            )
            return
        repository, number, branch = identity
        try:
            current = self.provider.get_pull_request(repository, number)
            target = self.review_provider.get_pull_request(attempt.pull_request_id)
        except Exception:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
                "Provider readback failed. Retry the fresh review cycle transition.",
            )
            return
        if (
            current.repository.casefold() != repository.casefold()
            or current.number != number
            or current.source_branch != branch
            or current.source_head != attempt.commit_sha
            or current.state != "OPEN"
            or current.merged
            or target.pull_request_id != attempt.pull_request_id
            or target.head_sha != attempt.commit_sha
            or not target.ready
        ):
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.HUMAN_ACTION_REQUIRED,
                "The current pull-request identity or head no longer matches the "
                "pushed repair. Verify the head and start a fresh review cycle "
                "manually.",
            )
            return
        try:
            self.reviews.start_repair_followup_cycle(attempt.attempt_id, target)
        except Exception:
            self.reviews.store.fail_repair_transition(
                attempt.attempt_id,
                ReviewRepairTransitionStatus.RETRY_REQUIRED,
                "The fresh review cycle could not be recorded. Retry the transition.",
            )

    def _prompt(self, attempt: ReviewRepairAttempt) -> str:
        findings = [
            self.reviews.store.finding_for_id(finding_id)
            for finding_id in attempt.finding_ids
        ]
        if any(
            finding.pull_request_id != attempt.pull_request_id
            or finding.head_sha != attempt.head_sha
            or finding.stale
            or finding.status is not FindingStatus.OPEN
            or finding.publication_state.value != "published"
            or finding.duplicate_target is not None
            for finding in findings
        ):
            raise ReviewError("Selected review findings are no longer current and open")
        payload = {
            "pull_request_id": attempt.pull_request_id,
            "head_sha": attempt.head_sha,
            "selected_findings": [
                {
                    "finding_id": finding.finding_id,
                    "concern": finding.concern.value,
                    "summary": finding.summary[:400],
                    "file_path": finding.file_path,
                    "start_line": finding.start_line,
                    "end_line": finding.end_line,
                    "evidence_refs": list(finding.evidence_refs[:2]),
                }
                for finding in findings
            ],
        }
        prompt = (
            "BeeHAIve selected review-finding repair. Repair only the findings in "
            "the JSON below. Work only in this leased worktree. Do not push, access "
            "credentials, edit another checkout, read unrelated review findings, "
            "or start another agent. Make the smallest correct change, run relevant "
            "checks, stage only the repair, and create one normal commit. Return a "
            "short plain-text result.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        prompt = self._safe_text(prompt, MAX_REPAIR_PROMPT_CHARS + 1)
        if len(prompt) > MAX_REPAIR_PROMPT_CHARS:
            raise ReviewError("Selected repair context exceeds the prompt limit")
        return prompt

    def _safe_text(self, value: str, limit: int) -> str:
        secrets = tuple(
            secret
            for secret in getattr(self.agent, "_secret_values", ())
            if isinstance(secret, str)
        )
        return redact_worker_text(value, secrets, max_length=limit)

    @staticmethod
    def _pull_request_parts(pull_request_id: str) -> tuple[str, int]:
        repository, separator, number_text = pull_request_id.rpartition("#")
        if not separator or not re.fullmatch(r"[^/#]+/[^/#]+", repository):
            raise ReviewError("Pull-request id must use owner/repository#number")
        if not number_text.isdigit() or int(number_text) <= 0:
            raise ReviewError("Pull-request number must be positive")
        return repository, int(number_text)

    @staticmethod
    def _validate_snapshot(
        snapshot: PullRequestSnapshot,
        attempt: ReviewRepairAttempt,
        repository: str,
        number: int,
    ) -> None:
        if (
            snapshot.repository.casefold() != repository.casefold()
            or snapshot.number != number
            or snapshot.state != "OPEN"
            or snapshot.merged
            or not snapshot.source_branch
            or snapshot.source_head != attempt.head_sha
        ):
            raise ProviderError("Pull request is not open at the selected finding head")

    @staticmethod
    def _same_source_identity(
        expected: PullRequestSnapshot,
        current: PullRequestSnapshot,
        *,
        expected_source_head: str | None = None,
    ) -> bool:
        return (
            expected.repository.casefold() == current.repository.casefold()
            and expected.number == current.number
            and expected.pull_request_id == current.pull_request_id
            and expected.source_branch == current.source_branch
            and current.source_head
            == (
                expected.source_head
                if expected_source_head is None
                else expected_source_head
            )
            and current.state == "OPEN"
            and not current.merged
        )

    def _validate_active_lease(self, lease: WorkspaceLease) -> None:
        current = self.workflow.store.get_lease(lease.lease_id)
        if (
            current is None
            or current.status is not LeaseStatus.ACTIVE
            or current.lease_token != lease.lease_token
        ):
            raise WorkflowError("Review repair workspace lease was lost")

    def _validate_push_remote(self, repository: str, worktree: str) -> None:
        result = self.workflow.worktrees.run_git(
            "-C", worktree, "remote", "get-url", "--push", "--all", "origin"
        )
        remote_urls = (
            tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
            if result.returncode == 0
            else ()
        )
        if (
            len(remote_urls) != 1
            or remote_urls != self.workflow.worktrees.origin_push_urls
            or (repository_identity(remote_urls[0]) or "").casefold()
            != repository.casefold()
        ):
            raise WorkflowError(
                "Configured push remote does not match the review repository"
            )

    @staticmethod
    def _raise_if_cancelled(attempt: ReviewRepairAttempt, cancelled: Event) -> None:
        if cancelled.is_set() or attempt.cancellation_requested:
            raise _RepairCancelled


class _RoutedRepairAgent:
    def __init__(
        self,
        agent: SelectedRepairAgent,
        attempt_id: str,
        worktree: Path,
        prompt: str,
        cancelled: Event,
    ) -> None:
        self.agent = agent
        self.attempt_id = attempt_id
        self.worktree = worktree
        self.prompt = prompt
        self.cancelled = cancelled

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        del decision
        return self.agent.execute_scoped_repair(
            self.attempt_id,
            self.worktree,
            self.prompt,
            spec.model,
            self.cancelled.is_set,
        )


class _RepairCancelled(Exception):
    pass
