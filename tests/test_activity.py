from __future__ import annotations

from beehaiive.activity import Agent, parse_skill_label, summarize_input
from beehaiive.records import Record
from tests.transcripts import (
    at,
    human,
    notification,
    text,
    tool_result,
    tool_use,
    ts,
)


def test_tool_call_becomes_a_timed_step() -> None:
    agent = Agent("s", "session")
    agent.apply(human("run the tests", 0, uuid="p1"))
    agent.apply(tool_use("t1", "Bash", {"command": "pytest -q"}, 1))
    agent.apply(tool_result("t1", 31))

    prompt, step = agent.steps
    assert prompt.kind == "prompt" and prompt.detail == "run the tests"
    assert step.name == "Bash" and step.detail == "pytest -q"
    assert step.status == "done"
    assert step.started_at == at(1) and step.ended_at == at(31)
    assert agent.open_steps() == []
    assert agent.cwd == "C:/work/repo" and agent.branch == "main"
    assert agent.started_at == at(0) and agent.last_activity == at(31)


def test_failed_tool_records_a_problem() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("t1", "Read", {"file_path": "missing.py"}, 0))
    agent.apply(tool_result("t1", 1, content="File does not exist", is_error=True))

    step = agent.steps[0]
    assert step.status == "failed" and step.error == "File does not exist"
    assert [(p.kind, p.step_id) for p in agent.problems] == [("tool_error", "t1")]


def test_repeated_or_unknown_tool_records_are_ignored() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("t1", "Grep", {"pattern": "x"}, 0))
    agent.apply(tool_use("t1", "Grep", {"pattern": "x"}, 0))
    agent.apply(tool_result("other", 1))
    agent.apply(tool_use("", "Grep", {}, 2))

    assert len(agent.steps) == 1
    assert agent.step("t1") is agent.steps[0]
    assert agent.step("nope") is None


def test_background_agent_stays_open_until_its_notification() -> None:
    agent = Agent("s", "session")
    launch: Record = {"description": "Review the diff", "subagent_type": "reviewer"}
    agent.apply(tool_use("a1", "Agent", launch, 0))
    agent.apply(
        tool_result("a1", 1, result={"status": "async_launched", "agentId": "child"})
    )

    step = agent.steps[0]
    assert step.kind == "agent" and step.detail == "Review the diff (reviewer)"
    assert step.status == "running" and step.child_agent_id == "child"

    agent.apply(notification("a1", 90, "completed", "all good"))
    assert step.status == "done" and step.ended_at == at(90)
    # A notification is not a human prompt.
    assert [s.kind for s in agent.steps] == ["agent"]


def test_failed_background_agent_is_a_problem() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("a1", "Task", {"description": "Build"}, 0))
    agent.apply(tool_result("a1", 1, result={"status": "async_launched"}))
    agent.apply(notification("a1", 5, "failed", "ran out of turns"))
    agent.apply(notification("unknown", 6, "failed"))

    step = agent.steps[0]
    assert step.status == "failed" and step.error == "ran out of turns"
    assert [p.kind for p in agent.problems] == ["agent_failed"]


def test_notification_without_summary_names_the_step() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("a1", "Agent", {"description": "Build"}, 0))
    agent.apply(tool_result("a1", 1, result={"status": "async_launched"}))
    agent.apply(notification("a1", 5, "killed"))

    assert agent.steps[0].error == "Build ended with killed"


def test_foreground_agent_closes_with_its_result() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("a1", "Agent", {"description": "Map"}, 0))
    agent.apply(tool_result("a1", 4, result={"status": "completed", "agentId": "c"}))

    assert agent.steps[0].status == "done"
    assert agent.steps[0].child_agent_id == "c"


def test_api_errors_and_hook_errors_are_problems() -> None:
    agent = Agent("s", "session")
    agent.apply(
        {
            "type": "assistant",
            "timestamp": ts(0),
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": "Overloaded"}]},
        }
    )
    agent.apply(
        {"type": "system", "timestamp": ts(1), "hookErrors": ["hook blocked", "", 3]}
    )
    agent.apply(
        {"type": "system", "timestamp": ts(2), "level": "error", "content": "boom"}
    )
    agent.apply({"type": "system", "timestamp": ts(3), "level": "info"})

    assert [(p.kind, p.message) for p in agent.problems] == [
        ("api_error", "Overloaded"),
        ("hook", "hook blocked"),
        ("api_error", "boom"),
    ]


def test_token_usage_counts_each_message_once() -> None:
    agent = Agent("s", "session")
    usage: Record = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 100,
        "output_tokens": 1,
    }
    agent.apply(tool_use("t1", "Bash", {"command": "a"}, 0, "m1", usage))
    agent.apply(
        tool_use("t2", "Bash", {"command": "b"}, 0, "m1", usage | {"output_tokens": 7})
    )
    later: Record = {"output_tokens": 3}
    agent.apply(tool_use("t3", "Bash", {"command": "c"}, 1, "m2", later))

    assert agent.input_tokens == 115
    assert agent.output_tokens == 10
    assert agent.model == "claude-test"


def test_titles_last_message_and_pull_requests() -> None:
    agent = Agent("s", "session")
    agent.apply({"type": "agent-name", "agentName": "named"})
    agent.apply({"type": "custom-title", "customTitle": "Chosen title"})
    agent.apply({"type": "agent-name", "agentName": "ignored"})
    agent.apply(text("Done with #12.", 3))
    agent.apply(text("   ", 4))
    pr: Record = {
        "type": "pr-link",
        "prNumber": 7,
        "prUrl": "https://x/pull/7",
        "prRepository": "o/r",
    }
    agent.apply(pr)
    agent.apply(pr)
    agent.apply({"type": "pr-link", "prNumber": 8})

    assert agent.title == "Chosen title"
    assert agent.last_message == "Done with #12."
    assert [(p.number, p.repository) for p in agent.pull_requests] == [(7, "o/r")]


def test_only_human_prompts_become_prompt_steps() -> None:
    agent = Agent("s", "session")
    agent.apply(human("hello", 0) | {"isMeta": True})
    agent.apply(human("from a hook", 1) | {"origin": {"kind": "hook"}})
    agent.apply(human("real", 2) | {"origin": {"kind": "human"}})

    assert [s.detail for s in agent.steps] == ["real"]


def test_pasted_images_and_queued_messages_are_prompts_too() -> None:
    agent = Agent("s", "session")
    blocks: Record = {
        "role": "user",
        "content": [
            {"type": "image", "source": {}},
            {"type": "text", "text": "look at this"},
        ],
    }
    agent.apply(human("", 0) | {"message": blocks})
    agent.apply(human("", 1) | {"message": {"content": [{"type": "image"}]}})
    queued: Record = {
        "type": "attachment",
        "timestamp": ts(2),
        "attachment": {
            "type": "queued_command",
            "prompt": "also this",
            "origin": {"kind": "human"},
        },
    }
    agent.apply(queued)
    agent.apply(queued | {"attachment": {"type": "hook_success"}})
    agent.apply(
        queued
        | {
            "attachment": {
                "type": "queued_command",
                "prompt": "x",
                "origin": {"kind": "task"},
            }
        }
    )

    assert [t.request for t in agent.turns] == ["look at this", "(image)", "also this"]


def test_turns_collect_tools_subagents_errors_and_reply() -> None:
    agent = Agent("s", "session")
    agent.apply(tool_use("t0", "Read", {"file_path": "a"}, 0))
    agent.apply(human("first", 1))
    agent.apply(tool_use("t1", "Bash", {"command": "x"}, 2))
    agent.apply(tool_result("t1", 3, "boom", is_error=True))
    agent.apply(tool_use("a1", "Agent", {"description": "help"}, 4))
    agent.apply(text("All done.", 9))
    agent.apply(human("second", 20))

    early, first, second = agent.turns
    assert early.request == "" and early.tool_counts == {"Read": 1}
    assert first.tool_counts == {"Bash": 1, "Agent": 1}
    assert first.agent_step_ids == ["a1"] and first.errors == 1
    assert first.reply == "All done."
    assert (first.started_at, first.ended_at) == (at(1), at(9))
    assert second.started_at == second.ended_at == at(20)


def test_subagent_label_comes_from_its_first_prompt() -> None:
    agent = Agent("c", "subagent")
    agent.apply(human("Skill: dod-guard:next-ticket #405 resume\nmore text", 0))
    agent.apply(human("Skill: dod-guard:other 9", 1))

    label = agent.label
    assert label is not None
    assert (label.skill, label.stage, label.issue) == (
        "dod-guard:next-ticket",
        "next-ticket",
        405,
    )
    assert label.args == "#405 resume"


def test_skill_tool_use_is_remembered() -> None:
    agent = Agent("s", "session")
    agent.apply(
        tool_use("k1", "Skill", {"skill": "dod-guard:goal-sdlc", "args": "go"}, 0)
    )
    agent.apply(tool_use("k2", "Skill", {}, 1))

    assert agent.skills_used == {"dod-guard:goal-sdlc"}
    assert agent.steps[0].detail == "dod-guard:goal-sdlc go"


def test_parse_skill_label_needs_a_skill_line() -> None:
    assert parse_skill_label("Please fix it") is None
    label = parse_skill_label("Skill: dod-guard:refine-backlog-item")
    assert label is not None and label.issue is None and label.args == ""
    dated = parse_skill_label("Skill: dod-guard:add-backlog-idea 2026-11-05 log")
    assert dated is not None and dated.issue is None


def test_summarize_input_picks_the_most_telling_field() -> None:
    assert summarize_input("Read", {"file_path": "a.py", "limit": 3}) == "a.py"
    assert summarize_input("WebFetch", {"url": "https://x"}) == "https://x"
    assert summarize_input("Bash", {"command": "ls  -la\n x"}) == "ls -la x"
    assert summarize_input("Edit", {"old_string": "", "new_string": "b"}) == "b"
    assert summarize_input("Mystery", {"n": 1}) == "Mystery"
    assert summarize_input("Mystery", None) == "Mystery"
    long = summarize_input("Bash", {"command": "x" * 500})
    assert len(long) == 240 and long.endswith("...")
