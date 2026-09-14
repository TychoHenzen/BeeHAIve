import subprocess
from pathlib import Path


def git_repository(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def make_repair_repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote = tmp_path / "owner" / "repo.git"
    remote.parent.mkdir()
    git_repository(tmp_path, "init", "--bare", "--initial-branch=master", str(remote))
    repository = tmp_path / "repository"
    repository.mkdir()
    git_repository(repository, "init", "-b", "master")
    git_repository(repository, "config", "user.email", "tests@example.test")
    git_repository(repository, "config", "user.name", "Review Repair Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git_repository(repository, "add", "README.md")
    git_repository(repository, "commit", "-m", "base")
    base_head = git_repository(repository, "rev-parse", "HEAD")
    git_repository(repository, "switch", "-c", "feature")
    (repository / "README.md").write_text("feature\n", encoding="utf-8")
    git_repository(repository, "add", "README.md")
    git_repository(repository, "commit", "-m", "feature")
    source_head = git_repository(repository, "rev-parse", "HEAD")
    git_repository(repository, "remote", "add", "origin", str(remote))
    git_repository(repository, "push", "origin", "master", "feature")
    return repository, remote, base_head, source_head
