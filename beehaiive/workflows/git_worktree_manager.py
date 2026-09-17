from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .helpers import require_text
from .lease_status import LeaseStatus
from .merge_result import MergeResult
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .workflow_store import WorkflowStore
from .workspace_lease import WorkspaceLease


def _resolve_git_executable() -> str:
    configured = os.environ.get("BEEHAIIVE_GIT_EXECUTABLE", "").strip()
    if configured:
        return configured
    discovered = shutil.which("git")
    if discovered and not any(
        marker in discovered.casefold() for marker in ("msys", "devkitpro")
    ):
        return discovered
    candidates = (
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Git"
        / "cmd"
        / "git.exe",
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "git"
        / "cmd"
        / "git.exe",
    )
    return next(
        (str(candidate) for candidate in candidates if candidate.is_file()),
        discovered or "git",
    )


class GitWorktreeManager:
    """Create and remove real git worktrees behind durable lease claims."""

    def __init__(
        self,
        repository: str | Path,
        store: WorkflowStore,
        git_timeout_seconds: float = 60.0,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.store = store
        self._git_executable = _resolve_git_executable()
        if git_timeout_seconds <= 0:
            raise WorkflowError("Git timeout must be positive")
        self.git_timeout_seconds = git_timeout_seconds
        if not self.repository.exists():
            raise WorkflowError(f"Repository does not exist: {self.repository}")
        self.host_git_identity = (
            self._config_value("user.name"),
            self._config_value("user.email"),
        )
        self.origin_push_urls = self._origin_urls()

    def _config_value(self, key: str) -> str | None:
        result = self._run_git("config", "--get", key)
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    def _origin_urls(self) -> tuple[str, ...]:
        result = self._run_git("remote", "get-url", "--push", "--all", "origin")
        if result.returncode != 0:
            return ()
        return tuple(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )

    def acquire(
        self, agent_id: str, branch: str, worktree: str | Path, base_ref: str = "HEAD"
    ) -> WorkspaceLease:
        branch = require_text(branch, "branch")
        base_ref = require_text(base_ref, "base ref")
        path = Path(worktree).resolve()
        for stale_lease in self.store.reclaim_expired():
            self._cleanup_stopped_worktree(stale_lease.worktree_path)
            self._delete_reclaimed_branch(stale_lease.branch)
        lease = self.store.acquire_lease(agent_id, branch, str(path))
        try:
            self._git("worktree", "add", "-b", branch, str(path), base_ref)
        except Exception:
            try:
                self._cleanup_stopped_worktree(str(path))
            except WorkflowError:
                pass
            finally:
                self.store.release_lease(lease.lease_id)
            raise
        return lease

    def release(self, lease_id: str) -> WorkspaceLease:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is LeaseStatus.ACTIVE:
            self.store.ensure_release_allowed(lease_id)
            self._git("worktree", "remove", lease.worktree_path)
            return self.store.release_lease(lease_id)
        if lease.status is LeaseStatus.RETAINED:
            if not self.clean(lease.worktree_path):
                raise WorkflowError(
                    "Cannot release a retained workspace with uncommitted changes"
                )
            self._git("worktree", "remove", lease.worktree_path)
            return self.store.release_lease(lease_id)
        if lease.status is LeaseStatus.STOPPED:
            self._cleanup_stopped_worktree(lease.worktree_path)
            self._delete_reclaimed_branch(lease.branch)
            return self.store.release_lease(lease_id, allow_stopped=True)
        return lease

    def head(self, worktree: str | Path) -> str:
        return self._git("-C", str(Path(worktree)), "rev-parse", "HEAD")

    def clean(self, worktree: str | Path) -> bool:
        return not self._git("-C", str(Path(worktree)), "status", "--porcelain")

    def fetch_exact_branch(
        self, branch: str, expected_head: str, destination_ref: str
    ) -> str:
        """Fetch one branch into an internal ref and prove its exact head."""

        branch = require_text(branch, "remote branch")
        expected_head = require_text(expected_head, "expected branch head", 200)
        destination_ref = require_text(destination_ref, "destination ref", 400)
        remotes = self._run_git("remote")
        if remotes.returncode != 0:
            message = (remotes.stderr or remotes.stdout).strip()
            raise WorkflowError(message or "Could not inspect repository remotes")
        remote_exists = "origin" in {
            line.strip() for line in remotes.stdout.splitlines()
        }
        if remote_exists:
            result = self._run_git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{branch}:{destination_ref}",
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip()
                raise WorkflowError(
                    message or "Could not fetch the pull-request branch"
                )
        else:
            if not self._commit_exists(expected_head):
                raise WorkflowError("Expected branch head is not available locally")
            self._git("update-ref", destination_ref, expected_head)
        actual = self._git("rev-parse", destination_ref)
        if actual != expected_head:
            raise WorkflowError("Remote branch head changed before repair started")
        return destination_ref

    def contains_commit(self, worktree: str | Path, commit: str) -> bool:
        result = self._run_git(
            "-C",
            str(Path(worktree)),
            "merge-base",
            "--is-ancestor",
            require_text(commit, "commit", 200),
            "HEAD",
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        message = (result.stderr or result.stdout).strip()
        raise WorkflowError(message or "Could not verify commit ancestry")

    def remove_ref(self, ref: str) -> None:
        try:
            self._git("update-ref", "-d", require_text(ref, "internal ref", 400))
        except WorkflowError:
            return

    def integrate_target(
        self, worktree: str | Path, expected_source_head: str, target_head: str
    ) -> MergeResult:
        workspace = Path(worktree)
        actual_source_head = self.head(workspace)
        if actual_source_head != expected_source_head:
            raise WorkflowError(
                "Repair worktree was not created from the expected head"
            )
        result = self._run_git(
            "-C",
            str(workspace),
            "merge",
            "--no-edit",
            "--no-ff",
            "--no-commit",
            target_head,
        )
        evidence = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )[-4_000:]
        if result.returncode == 0:
            return MergeResult(False, evidence or "Target branch staged for merge")
        conflicts = self._run_git(
            "-C",
            str(workspace),
            "diff",
            "--name-only",
            "--diff-filter=U",
        )
        if conflicts.returncode == 0 and conflicts.stdout.strip():
            return MergeResult(
                True,
                f"Merge conflicts: {conflicts.stdout.strip()}"[-4_000:],
            )
        raise WorkflowError(evidence or "Target branch integration failed")

    def _git(self, *arguments: str) -> str:
        result = self._run_git(*arguments)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise WorkflowError(message or f"git command failed: {' '.join(arguments)}")
        return result.stdout.strip()

    def run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run one bounded, no-shell Git command for WorkflowService."""

        return self._run_git(*arguments)

    def _run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                (self._git_executable, *arguments),
                cwd=self.repository,
                capture_output=True,
                text=True,
                timeout=self.git_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                f"git command timed out after {self.git_timeout_seconds:g} seconds"
            ) from exc
        return result

    def _commit_exists(self, commit: str) -> bool:
        result = self._run_git("cat-file", "-t", commit)
        return result.returncode == 0 and result.stdout.strip() == "commit"

    def _cleanup_stopped_worktree(self, worktree_path: str) -> None:
        path = Path(worktree_path)
        if not path.exists():
            self._git("worktree", "prune")
            return
        self._git("worktree", "remove", "--force", str(path))

    def _delete_reclaimed_branch(self, branch: str) -> None:
        if not self._git("branch", "--list", branch):
            return
        current = self._git("branch", "--show-current")
        if current == branch:
            raise WorkflowError(f"Cannot reclaim the checked-out branch: {branch}")
        self._git("branch", "-D", branch)
