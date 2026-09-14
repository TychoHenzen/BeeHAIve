from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from beehaiive.models import PullRequestSnapshot
from beehaiive.provider import ProviderError


class FakeProvider:
    def __init__(self, snapshot: PullRequestSnapshot) -> None:
        self.snapshot = snapshot
        self.update_calls = 0
        self.get_calls = 0
        self.change_before_update = False
        self.confirm_bad = False

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        assert repository == self.snapshot.repository
        assert number == self.snapshot.number
        self.get_calls += 1
        if self.change_before_update and self.get_calls > 1 and self.update_calls == 0:
            return replace(self.snapshot, source_head="changed-head")
        return self.snapshot

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        del worktree
        if snapshot.source_head != expected_head:
            raise ProviderError("source head changed")
        self.update_calls += 1
        if not self.confirm_bad:
            self.snapshot = replace(
                snapshot,
                source_head=repaired_head,
                mergeable="MERGEABLE",
                merge_state="CLEAN",
            )
