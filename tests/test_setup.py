import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_the_clean_device_demo_path() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_sections = (
        "uv sync",
        "GITHUB_TOKEN",
        "GITHUB_PROJECT_OWNER",
        "BEEHAIIVE_ALLOWED_PROJECTS",
        "BEEHAIIVE_API_KEY",
        "BEEHAIIVE_AGENT_REPOSITORY",
        "/dashboard?project=<owner>:<number>",
        "Start writer",
        "Result",
        "Failure",
        "Stop the run before removing local `.beehaiive` state.",
        "dashboard-configured.png",
        "active-demo.png",
        "completed-demo.png",
        "stopped-demo.png",
    )
    assert all(section in readme for section in required_sections)
    assert 'GITHUB_TOKEN = "ghp_' not in readme
    assert 'BEEHAIIVE_API_KEY = "' not in readme


def test_batch_launcher_is_rooted_and_actionable() -> None:
    launcher = (ROOT / "start_dashboard.bat").read_text(encoding="utf-8")

    assert 'cd /d "%~dp0"' in launcher
    assert 'if exist ".env"' in launcher
    assert "eol=# tokens=1,* delims==" in launcher
    assert "where uv" in launcher
    assert "where codex" in launcher
    assert "GITHUB_TOKEN or GH_TOKEN" in launcher
    assert "GITHUB_PROJECT_OWNER" in launcher
    assert "GITHUB_PROJECT_NUMBER" in launcher
    assert "BEEHAIIVE_API_KEY" in launcher
    assert "uv run uvicorn main:app --reload" in launcher


@pytest.mark.skipif(os.name != "nt", reason="The launcher is Windows-specific")
def test_batch_launcher_loads_dotenv_values(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "# launcher test",
                "GITHUB_TOKEN=dotenv-github-token",
                "GITHUB_PROJECT_OWNER=dotenv-owner",
                "GITHUB_PROJECT_NUMBER=42",
                "BEEHAIIVE_API_KEY=dotenv-api-key",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    (command_dir / "uv.cmd").write_text(
        "@echo off\n"
        "echo token=%GITHUB_TOKEN%\n"
        "echo owner=%GITHUB_PROJECT_OWNER%\n"
        "echo number=%GITHUB_PROJECT_NUMBER%\n"
        "echo api-key=%BEEHAIIVE_API_KEY%\n",
        encoding="utf-8",
    )
    (command_dir / "codex.cmd").write_text("@echo off\n", encoding="utf-8")
    launcher = tmp_path / "start_dashboard.bat"
    shutil.copy2(ROOT / "start_dashboard.bat", launcher)

    environment = os.environ.copy()
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PROJECT_OWNER",
        "GITHUB_PROJECT_NUMBER",
        "BEEHAIIVE_API_KEY",
    ):
        environment.pop(name, None)
    environment["PATH"] = f"{command_dir}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        ["cmd", "/d", "/c", str(launcher)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "token=dotenv-github-token" in result.stdout
    assert "owner=dotenv-owner" in result.stdout
    assert "number=42" in result.stdout
    assert "api-key=dotenv-api-key" in result.stdout
