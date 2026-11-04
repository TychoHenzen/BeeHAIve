from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from beehaiive.agents import AgentService, _remote_repository, _skill_prompt
from tests.agent_support import StreamingProcess, issue_card


def test_streaming_process_keeps_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    process = StreamingProcess()
    log_path = tmp_path / "step.jsonl"

    result = AgentService._communicate_process(
        AgentService.__new__(AgentService),
        process,
        "prompt",
        log_path,
        timeout=1,
    )

    assert result == 7
    assert process.stdin.value == "prompt"
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert {(event["event"], event["line"]) for event in events} == {
        ("stdout", "stdout first"),
        ("stdout", "stdout second"),
        ("stderr", "stderr first"),
    }


def test_remote_repository_accepts_github_urls_only() -> None:
    assert _remote_repository("https://github.com/Owner/Repo.git") == "Owner/Repo"
    assert _remote_repository("git@github.com:Owner/Repo.git") == "Owner/Repo"
    assert _remote_repository("https://gitlab.com/Owner/Repo.git") is None
    assert _remote_repository("") is None


def test_skill_prompt_exposes_real_skill_path_and_child_authority(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "deliver" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("deliver", encoding="utf-8")
    card = replace(issue_card(229, "runner"), url=None)

    prompt = _skill_prompt(
        {"skill": "deliver", "prompt": "Work the item.", "outcomes": ["done"]},
        card,
        "Todo",
        {},
        skills_dirs=(tmp_path,),
    )

    assert str(skill_path.resolve()) in prompt
    assert "authorized gh/git workflow" in prompt
    assert "Return blocked only when authority" in prompt
