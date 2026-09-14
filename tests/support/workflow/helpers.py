from __future__ import annotations

import subprocess
from pathlib import Path

from beehaiive.workflow import (
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from tests.support.workflow.fixture_check import FixtureCheck as FixtureCheck

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


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    git_command(repository, "init", "-b", "master")
    git_command(repository, "config", "user.email", "tests@example.test")
    git_command(repository, "config", "user.name", "Workflow Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git_command(repository, "add", "README.md")
    git_command(repository, "commit", "-m", "base")
    return repository


def commit_repository_change(worktree: Path, name: str, content: str) -> str:
    (worktree / name).write_text(content, encoding="utf-8")
    git_command(worktree, "add", name)
    git_command(worktree, "commit", "-m", f"add {name}")
    return git_command(worktree, "rev-parse", "HEAD")


def make_service(
    tmp_path: Path,
    checks: list[FixtureCheck] | None = None,
) -> tuple[WorkflowService, WorkflowStore, Path]:
    repository = make_repository(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        checks or [FixtureCheck("tests")],
    )
    return service, store, repository


def make_delivery_service(
    tmp_path: Path,
    *,
    identity: bool = True,
    push_remote: Path | None = None,
) -> tuple[WorkflowService, WorkflowStore, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = make_repository(tmp_path)
    remote = tmp_path / "owner" / "api.git"
    remote.parent.mkdir()
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    git_command(repository, "remote", "add", "origin", str(remote))
    git_command(repository, "push", "origin", "master")
    if push_remote is not None:
        push_remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ("git", "init", "--bare", str(push_remote)),
            check=True,
            capture_output=True,
        )
        git_command(
            repository, "remote", "set-url", "--push", "origin", str(push_remote)
        )
    if not identity:
        git_command(repository, "config", "--unset", "user.name")
        git_command(repository, "config", "--unset", "user.email")
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck("tests")],
    )
    return service, store, repository, remote
