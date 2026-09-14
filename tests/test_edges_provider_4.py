from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    PullRequestSnapshot,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    ProviderError,
    _dashboard_metadata,
)
from tests.support.edges.helpers import git_command as _git
from tests.support.edges.static_client import StaticClient as StaticClient


def test_provider_updates_only_the_expected_source_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Provider Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    _git(repository, "switch", "-c", "feature")
    (repository / "README.md").write_text("source\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "source")
    expected_head = _git(repository, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "--all")

    worktree = tmp_path / "repair"
    _git(repository, "worktree", "add", "-b", "repair", str(worktree), expected_head)
    (worktree / "repair.txt").write_text("repaired\n", encoding="utf-8")
    _git(worktree, "add", "repair.txt")
    _git(worktree, "commit", "-m", "repair")
    repaired_head = _git(worktree, "rev-parse", "HEAD")
    snapshot = PullRequestSnapshot(
        "owner/api",
        1,
        "PR_1",
        "https://example.test/pull/1",
        "OPEN",
        False,
        "feature",
        expected_head,
        "master",
        _git(repository, "rev-parse", "master"),
        "CONFLICTING",
        "DIRTY",
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=StaticClient({}))

    provider.update_source_branch(snapshot, worktree, expected_head, repaired_head)

    assert _git(remote, "rev-parse", "refs/heads/feature") == repaired_head
    _git(repository, "switch", "-c", "advance", repaired_head)
    (repository / "advance.txt").write_text("advance\n", encoding="utf-8")
    _git(repository, "add", "advance.txt")
    _git(repository, "commit", "-m", "advance remote branch")
    _git(repository, "push", "--force", "origin", "advance:feature")
    _git(repository, "switch", "feature")
    _git(repository, "branch", "-D", "advance")
    with pytest.raises(ProviderError, match="exit code"):
        provider.update_source_branch(snapshot, worktree, expected_head, repaired_head)
    stale = replace(snapshot, source_head="different-head")
    with pytest.raises(ProviderError, match="changed"):
        provider.update_source_branch(stale, worktree, expected_head, repaired_head)
    with pytest.raises(ProviderError, match="eligible"):
        provider.update_source_branch(
            replace(snapshot, state="CLOSED"), worktree, expected_head, repaired_head
        )
    with pytest.raises(ProviderError, match="worktree head"):
        provider.update_source_branch(snapshot, worktree, expected_head, expected_head)

    _git(repository, "worktree", "remove", "--force", str(worktree))
    _git(repository, "branch", "-D", "repair")


def test_provider_guarded_update_rejects_unrelated_history_and_git_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Provider Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    base_head = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "repair"
    _git(repository, "worktree", "add", "-b", "repair", str(worktree), base_head)
    (repository / "expected.txt").write_text("expected\n", encoding="utf-8")
    _git(repository, "add", "expected.txt")
    _git(repository, "commit", "-m", "expected")
    expected_head = _git(repository, "rev-parse", "HEAD")
    (worktree / "unrelated.txt").write_text("other\n", encoding="utf-8")
    _git(worktree, "add", "unrelated.txt")
    _git(worktree, "commit", "-m", "unrelated")
    unrelated_head = _git(worktree, "rev-parse", "HEAD")
    snapshot = PullRequestSnapshot(
        "owner/api",
        1,
        "PR_1",
        "https://example.test/pull/1",
        "OPEN",
        False,
        "feature",
        expected_head,
        "master",
        expected_head,
        "CONFLICTING",
        "DIRTY",
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=StaticClient({}))
    with pytest.raises(ProviderError, match="preserve source"):
        provider.update_source_branch(snapshot, worktree, expected_head, unrelated_head)

    _git(repository, "worktree", "remove", "--force", str(worktree))
    _git(repository, "branch", "-D", "repair")

    def broken_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("git unavailable")

    monkeypatch.setattr(provider_module.subprocess, "run", broken_run)
    with pytest.raises(ProviderError, match="Guarded source"):
        provider.update_source_branch(snapshot, worktree, expected_head, unrelated_head)


def test_provider_preserves_archive_evidence() -> None:
    metadata = _dashboard_metadata(
        {
            "url": "https://example.test/issues/1",
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": 1,
                        "url": "https://example.test/pull/1",
                        "state": "MERGED",
                        "merged": True,
                        "headRefName": "codex/done",
                        "headRef": None,
                    },
                    {
                        "number": 2,
                        "headRefName": "codex/live",
                        "headRef": {"name": "codex/live"},
                    },
                    {
                        "number": 3,
                        "headRefName": "codex/malformed",
                        "headRef": "not-an-object",
                    },
                    {"number": 4, "headRefName": "codex/unknown"},
                    {"number": 5, "headRef": None},
                    {"number": 6, "headRefName": "", "headRef": None},
                ]
            },
        }
    )

    pull_requests = metadata["pull_requests"]
    assert metadata["source_url"] == "https://example.test/issues/1"
    assert [pull_request["source_branch_state"] for pull_request in pull_requests] == [
        "deleted",
        "present",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
    ]  # type: ignore[index]
    assert pull_requests[0]["source_branch"] == "codex/done"  # type: ignore[index]
