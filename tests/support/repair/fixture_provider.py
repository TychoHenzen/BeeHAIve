from __future__ import annotations

from pathlib import Path

from beehaiive.models import PullRequestSnapshot
from tests.support.repair.repository import git_repository


class FixtureProvider:
    def __init__(
        self,
        remote: Path,
        repository: Path,
        base_head: str,
        source_head: str,
        *,
        change_on_call: int | None = None,
    ) -> None:
        self.remote = remote
        self.repository = repository
        self.base_head = base_head
        self.source_head = source_head
        self.change_on_call = change_on_call
        self.get_calls = 0
        self.update_calls = 0

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        assert repository == "owner/repo"
        assert number == 1
        self.get_calls += 1
        head = git_repository(self.remote, "rev-parse", "refs/heads/feature")
        if self.change_on_call == self.get_calls:
            head = "externally-updated-head"
        return PullRequestSnapshot(
            repository,
            number,
            "PULL_REQUEST_NODE",
            "https://github.com/owner/repo/pull/1",
            "OPEN",
            False,
            "feature",
            head,
            "master",
            self.base_head,
            "MERGEABLE",
            "CLEAN",
        )

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        if snapshot.source_head != expected_head:
            raise RuntimeError("source head changed")
        git_repository(
            Path(worktree),
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/feature:{expected_head}",
            "origin",
            f"{repaired_head}:refs/heads/feature",
        )
        self.update_calls += 1
