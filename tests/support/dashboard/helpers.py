import subprocess
from pathlib import Path

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
)


def make_dashboard_git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Dashboard Tests"),
    ):
        result = subprocess.run(
            ("git", *arguments), cwd=path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr or result.stdout
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=path, check=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path


def configure_dashboard_remote(repository: Path, root: Path) -> Path:
    remote = root / "owner" / "api.git"
    remote.parent.mkdir()
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    subprocess.run(
        ("git", "remote", "add", "origin", str(remote)),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "push", "origin", "master"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return remote


def dashboard_snapshot(
    project_id: str = "project-1",
    api_title: str = "API one",
    extra_pbis: tuple[PbiSnapshot, ...] = (),
) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id,
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        api_title,
                        metadata={
                            "subtasks": [{"id": "1a", "title": "Check API"}],
                            "readers": [
                                {"id": "security", "status": "pass"},
                                {"id": "tests", "status": "pending"},
                            ],
                            "reviewers": {
                                "security": {"status": "pass"},
                                "tests": {"status": "pending"},
                            },
                            "escalation": {
                                "current": 1,
                                "consecutive": 1,
                            },
                            "escalation_log": [{"tier": "terra"}],
                            "activity": [{"time": "now", "action": "Started review"}],
                        },
                    ),
                    PbiSnapshot("owner/api", 4, "API four"),
                    *extra_pbis,
                ),
            ),
            RepositorySnapshot(
                "owner/web",
                (
                    PbiSnapshot("owner/web", 2, "Web one"),
                    PbiSnapshot("owner/web", 3, "Web two"),
                ),
            ),
        ),
    )
