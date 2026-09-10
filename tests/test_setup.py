from pathlib import Path

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
    assert "where uv" in launcher
    assert "where codex" in launcher
    assert "GITHUB_TOKEN or GH_TOKEN" in launcher
    assert "GITHUB_PROJECT_OWNER" in launcher
    assert "GITHUB_PROJECT_NUMBER" in launcher
    assert "BEEHAIIVE_API_KEY" in launcher
    assert "uv run uvicorn main:app --reload" in launcher
