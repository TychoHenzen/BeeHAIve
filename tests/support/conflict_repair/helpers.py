from __future__ import annotations

import subprocess
from pathlib import Path

from beehaiive.models import PullRequestSnapshot

CONSTITUTION_PATH = Path(__file__).resolve().parents[3] / "constitution.json"


def git_command(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def make_conflict_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    git_command(repository, "init", "-b", "master")
    git_command(repository, "config", "user.email", "tests@example.test")
    git_command(repository, "config", "user.name", "Conflict Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git_command(repository, "add", "README.md")
    git_command(repository, "commit", "-m", "base")
    git_command(repository, "switch", "-c", "feature")
    (repository / "README.md").write_text("source\n", encoding="utf-8")
    git_command(repository, "add", "README.md")
    git_command(repository, "commit", "-m", "source")
    source_head = git_command(repository, "rev-parse", "HEAD")
    git_command(repository, "switch", "master")
    git_command(repository, "switch", "-c", "target")
    (repository / "README.md").write_text("target\n", encoding="utf-8")
    git_command(repository, "add", "README.md")
    git_command(repository, "commit", "-m", "target")
    target_head = git_command(repository, "rev-parse", "HEAD")
    git_command(repository, "switch", "feature")
    return repository, source_head, target_head


def pull_request_snapshot(source_head: str, target_head: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repository="owner/repo",
        number=7,
        pull_request_id="PR_7",
        url="https://example.test/pull/7",
        state="OPEN",
        merged=False,
        source_branch="feature",
        source_head=source_head,
        target_branch="target",
        target_head=target_head,
        mergeable="CONFLICTING",
        merge_state="DIRTY",
    )
