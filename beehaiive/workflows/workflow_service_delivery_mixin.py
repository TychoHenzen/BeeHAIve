from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .check_result import CheckResult
from .gate_result import GateResult
from .git_delivery_result import GitDeliveryResult
from .git_delivery_status import GitDeliveryStatus
from .helpers import repository_identity, require_text, required_checks_pass
from .lease_status import LeaseStatus
from .workflow_error import WorkflowError

# ponytail: keep the guarded commit and push together until replay tests permit a split.


class WorkflowServiceDeliveryMixin:
    def commit_and_push(
        self: Any,
        lease_id: str,
        lease_token: str | None,
        expected_agent_id: str,
        expected_repository: str,
        commit_message: str,
        validate_run: Callable[[], None],
    ) -> GitDeliveryResult:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status not in {LeaseStatus.ACTIVE, LeaseStatus.RETAINED}:
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        if not lease_token or lease.lease_token != lease_token:
            raise WorkflowError("Lease token is invalid")
        if lease.agent_id != expected_agent_id:
            raise WorkflowError("Workspace lease does not belong to this run")
        try:
            normalized_message = require_text(commit_message, "commit message", 200)
        except WorkflowError:
            normalized_message = ""

        def validate_pair() -> None:
            current = self.store.get_lease(lease_id)
            if current is None or current.status not in {
                LeaseStatus.ACTIVE,
                LeaseStatus.RETAINED,
            }:
                state = "missing" if current is None else current.status.value
                raise WorkflowError(f"Workspace lease is {state}")
            if current.lease_token != lease_token:
                raise WorkflowError("Lease token is invalid")
            if current.agent_id != expected_agent_id:
                raise WorkflowError("Workspace lease does not belong to this run")
            validate_run()

        def git_text(*arguments: str) -> str | None:
            try:
                result = self.worktrees.run_git(*arguments)
            except (OSError, WorkflowError):
                return None
            return result.stdout.strip() if result.returncode == 0 else None

        def target_problem(*, validate_remote: bool) -> str | None:
            workspace = Path(lease.worktree_path).resolve()
            if workspace == self.worktrees.repository or not workspace.is_dir():
                return "The leased worktree path is unavailable or not isolated."
            prefix = git_text("-C", str(workspace), "rev-parse", "--show-prefix")
            if prefix is None:
                git_pointer = workspace / ".git"
                if git_pointer.is_file():
                    pointer = git_pointer.read_text(encoding="utf-8").strip()
                    if pointer.casefold().startswith("gitdir:"):
                        worktree_git_path = Path(pointer[7:].strip())
                        if not worktree_git_path.is_absolute():
                            worktree_git_path = workspace / worktree_git_path
                        root_common = git_text("rev-parse", "--git-common-dir")
                        if root_common is not None:
                            root_common_path = Path(root_common)
                            if not root_common_path.is_absolute():
                                root_common_path = (
                                    self.worktrees.repository / root_common_path
                                )
                            if (
                                root_common_path.resolve()
                                not in worktree_git_path.resolve().parents
                            ):
                                return (
                                    "The leased worktree belongs to a different "
                                    "repository."
                                )
                return "The leased worktree could not be opened as a Git worktree."
            if prefix:
                return "The exact leased worktree could not be verified."
            root_common = git_text("rev-parse", "--git-common-dir")
            worktree_common = git_text(
                "-C", str(workspace), "rev-parse", "--git-common-dir"
            )
            if root_common is None or worktree_common is None:
                return "The leased worktree repository could not be verified."
            root_common_path = Path(root_common)
            if not root_common_path.is_absolute():
                root_common_path = self.worktrees.repository / root_common_path
            worktree_common_path = Path(worktree_common)
            if not worktree_common_path.is_absolute():
                worktree_common_path = workspace / worktree_common_path
            if (
                root_common_path.resolve().as_posix().casefold()
                != worktree_common_path.resolve().as_posix().casefold()
            ):
                return "The leased worktree belongs to a different repository."
            branch = git_text("-C", str(workspace), "branch", "--show-current")
            if not branch:
                return "Detached HEAD is not allowed for delivery."
            if branch != lease.branch:
                return "The current branch does not match the workspace lease."
            if validate_remote:
                remote_result = self.worktrees.run_git(
                    "-C",
                    str(workspace),
                    "remote",
                    "get-url",
                    "--push",
                    "--all",
                    "origin",
                )
                remote_urls = (
                    tuple(
                        line.strip()
                        for line in remote_result.stdout.splitlines()
                        if line.strip()
                    )
                    if remote_result.returncode == 0
                    else ()
                )
                if (
                    len(remote_urls) != 1
                    or remote_urls != self.worktrees.origin_push_urls
                    or (repository_identity(remote_urls[0]) or "").casefold()
                    != expected_repository.casefold()
                ):
                    return (
                        "The configured push remote does not match the leased "
                        "repository."
                    )
            return None

        quality_checks: tuple[CheckResult, ...] = ()

        def record(
            status: GitDeliveryStatus,
            delivery_state: str,
            commit_sha: str | None,
            evidence: str,
        ) -> GitDeliveryResult:
            validate_pair()
            checks = (
                *quality_checks,
                CheckResult(
                    "delivery_status",
                    status is not GitDeliveryStatus.BLOCKED,
                    delivery_state,
                ),
                CheckResult("commit_sha", commit_sha is not None, commit_sha or ""),
                CheckResult("branch", True, lease.branch),
                CheckResult(
                    "evidence", status is not GitDeliveryStatus.BLOCKED, evidence
                ),
            )
            self.store.record_gate(
                lease_id,
                GateResult(
                    "git_delivery",
                    status is not GitDeliveryStatus.BLOCKED,
                    checks,
                    None
                    if status is not GitDeliveryStatus.BLOCKED
                    else "Retry commit and push after resolving the delivery blocker",
                ),
            )
            return GitDeliveryResult(
                status, lease_id, lease.branch, commit_sha, evidence
            )

        validate_pair()
        problem = target_problem(validate_remote=False)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
        status_result = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain", "--untracked-files=all"
        )
        if status_result.returncode != 0:
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                None,
                "The leased worktree status could not be verified.",
            )
        dirty = bool(status_result.stdout.strip())
        head = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
        if head is None:
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                None,
                "The leased worktree HEAD could not be verified.",
            )
        previous = self.store.latest_gate(lease_id, "git_delivery")
        previous_checks = (
            {check.name: check.evidence for check in previous.checks}
            if previous is not None
            else {}
        )
        previous_state = previous_checks.get("delivery_status")
        previous_sha = previous_checks.get("commit_sha") or None
        quality_checks = tuple(self.checks.run(Path(lease.worktree_path)))
        if not required_checks_pass(quality_checks):
            if previous_state == GitDeliveryStatus.PUSHED.value and previous_sha:
                if not dirty and head == previous_sha:
                    blocked_state, blocked_sha = previous_state, previous_sha
                elif not dirty:
                    blocked_state, blocked_sha = "blocked", head
                else:
                    blocked_state, blocked_sha = "blocked", None
            elif (
                previous_state in {"push_pending", "push_failed", "blocked"}
                and previous_sha
            ):
                blocked_state, blocked_sha = previous_state, previous_sha
            else:
                blocked_state, blocked_sha = "blocked", None
            failed_names = ", ".join(
                check.name
                for check in quality_checks
                if check.required and not check.passed
            )
            return record(
                GitDeliveryStatus.BLOCKED,
                blocked_state,
                blocked_sha,
                "Required quality gates failed before Git delivery: " + failed_names,
            )
        if previous_state == GitDeliveryStatus.PUSHED.value and previous_sha:
            if not dirty and head == previous_sha:
                return record(
                    GitDeliveryStatus.PUSHED,
                    GitDeliveryStatus.PUSHED.value,
                    previous_sha,
                    "The recorded commit was already pushed.",
                )
            if not dirty:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    head,
                    "The worktree HEAD changed after the previous push; retry to "
                    "deliver the new commit.",
                )
        if previous_state == GitDeliveryStatus.NO_CHANGES.value and not dirty:
            return record(
                GitDeliveryStatus.NO_CHANGES,
                GitDeliveryStatus.NO_CHANGES.value,
                None,
                "The working tree was clean; no commit or push was needed.",
            )
        retry_commit = previous_state in {
            "push_pending",
            "push_failed",
            "blocked",
        } and bool(previous_sha)
        if not dirty and not retry_commit:
            return record(
                GitDeliveryStatus.NO_CHANGES,
                GitDeliveryStatus.NO_CHANGES.value,
                None,
                "The working tree was clean; no commit or push was needed.",
            )
        if retry_commit and (dirty or head != previous_sha):
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                previous_sha,
                "The saved commit no longer matches the clean leased worktree.",
            )
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", previous_sha, problem)

        commit_sha = previous_sha if retry_commit else None
        if not retry_commit:
            name, email = self.worktrees.host_git_identity
            if not name or not email:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Host Git user.name and user.email are required before commit.",
                )
            if not normalized_message or any(
                ord(character) < 32 for character in normalized_message
            ):
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "A valid commit message is required before staging.",
                )
            validate_pair()
            problem = target_problem(validate_remote=True)
            if problem:
                return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
            staged = self.worktrees.run_git(
                "-C", lease.worktree_path, "add", "-A", "--", "."
            )
            if staged.returncode != 0:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Changes could not be staged in the leased worktree.",
                )
            diff = self.worktrees.run_git(
                "-C", lease.worktree_path, "diff", "--cached", "--quiet"
            )
            if diff.returncode == 0:
                return record(
                    GitDeliveryStatus.NO_CHANGES,
                    GitDeliveryStatus.NO_CHANGES.value,
                    None,
                    "The working tree had no committable changes.",
                )
            if diff.returncode != 1:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Staged changes could not be verified.",
                )
            validate_pair()
            problem = target_problem(validate_remote=True)
            if problem:
                return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
            committed = self.worktrees.run_git(
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "-C",
                lease.worktree_path,
                "commit",
                "-m",
                normalized_message,
            )
            if committed.returncode != 0:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Git could not create the commit; staged work remains recoverable.",
                )
            commit_sha = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
            if commit_sha is None:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "The new commit could not be verified; local work remains "
                    "recoverable.",
                )
            clean_result = self.worktrees.run_git(
                "-C", lease.worktree_path, "status", "--porcelain"
            )
            if clean_result.returncode != 0 or clean_result.stdout.strip():
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    commit_sha,
                    "The commit exists, but the worktree is not clean; push was "
                    "skipped.",
                )
            record(
                GitDeliveryStatus.BLOCKED,
                "push_pending",
                commit_sha,
                "The local commit is recorded and ready to push.",
            )

        validate_pair()
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", commit_sha, problem)
        current_head = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
        current_status = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain"
        )
        if (
            current_head != commit_sha
            or current_status.returncode != 0
            or current_status.stdout.strip()
        ):
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                commit_sha,
                "Push requires the recorded commit and a clean leased worktree.",
            )
        push_url = self.worktrees.origin_push_urls[0]
        pushed = self.worktrees.run_git(
            "-C",
            lease.worktree_path,
            "push",
            "--porcelain",
            push_url,
            f"{commit_sha}:refs/heads/{lease.branch}",
        )
        if pushed.returncode != 0:
            return record(
                GitDeliveryStatus.BLOCKED,
                "push_failed",
                commit_sha,
                "Push failed; the local commit is preserved and can be retried.",
            )
        validate_pair()
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "push_failed", commit_sha, problem)
        remote_head = self.worktrees.run_git(
            "-C",
            lease.worktree_path,
            "ls-remote",
            "--exit-code",
            push_url,
            f"refs/heads/{lease.branch}",
        )
        remote_rows = [line.split("\t", 1) for line in remote_head.stdout.splitlines()]
        local_clean = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain"
        )
        if (
            remote_head.returncode != 0
            or len(remote_rows) != 1
            or remote_rows[0] != [commit_sha, f"refs/heads/{lease.branch}"]
            or git_text("-C", lease.worktree_path, "rev-parse", "HEAD") != commit_sha
            or local_clean.returncode != 0
            or local_clean.stdout.strip()
        ):
            return record(
                GitDeliveryStatus.BLOCKED,
                "push_failed",
                commit_sha,
                "Push could not be verified; the local commit remains preserved.",
            )
        return record(
            GitDeliveryStatus.PUSHED,
            GitDeliveryStatus.PUSHED.value,
            commit_sha,
            f"Remote branch {lease.branch} was verified at the recorded commit.",
        )
