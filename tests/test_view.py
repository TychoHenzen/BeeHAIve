from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from beehaiive.activity import Agent
from beehaiive.records import Record
from beehaiive.repos import RepoResolver
from beehaiive.view import SessionView
from tests.transcripts import at, human, text, tool_result, tool_use


def obj(value: object) -> Record:
    assert isinstance(value, dict)
    return cast(Record, value)


def items(value: object) -> list[Record]:
    assert isinstance(value, list)
    return [obj(item) for item in cast(list[object], value)]


def resolver(open_numbers: list[int] | None = None) -> RepoResolver:
    nodes = [{"number": n} for n in open_numbers or []]
    payload = json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {"totalCount": len(nodes), "nodes": nodes},
                    "pullRequests": {"totalCount": 0, "nodes": []},
                }
            }
        }
    )

    def run(args: Sequence[str]) -> str | None:
        if args[0] == "git":
            return "https://github.com/o/r.git\n"
        return payload if open_numbers is not None else None

    repos = RepoResolver(run)
    repo = repos.repository("C:/work/repo")
    assert repo is not None
    repos.repo_state(repo)
    return repos


def subagent(
    agent_id: str,
    records: list[Record],
    launch: str | None = None,
    description: str = "",
    finished: bool = True,
) -> Agent:
    agent = Agent(agent_id, "subagent", description=description)
    agent.parent_tool_use_id = launch
    for record in records:
        agent.apply(record)
    agent.finished = finished
    return agent


def goal_sdlc_view(open_numbers: list[int] | None = None) -> SessionView:
    root = Agent("s", "session")
    root.apply(human("/goal-sdlc", 0))
    root.apply(tool_use("k", "Skill", {"skill": "dod-guard:goal-sdlc"}, 1))
    stopped_then_rerun = subagent(
        "c1",
        [
            human("Skill: dod-guard:next-ticket 405\nGo", 10),
            text("Skill loaded: x. Stop reason: CI red.", 70),
        ],
    )
    rerun = subagent(
        "c2",
        [human("Skill: dod-guard:next-ticket 405", 100), text("Merged. Done.", 400)],
    )
    stopped = subagent(
        "c3",
        [
            human("Open the draft", 500),
            tool_use("g", "Agent", {"description": "check"}, 510),
            text("Stop reason: duplicate evidence", 560),
        ],
        description="submit-draft-pr 406",
    )
    quiet_grandchild = subagent("g1", [human("check", 511)], launch="g", finished=False)
    upkeep = subagent(
        "u1", [human("Refresh", 600), text("ok", 660)], description="Refresh queue"
    )
    agents = [stopped_then_rerun, rerun, stopped, quiet_grandchild, upkeep]
    return SessionView(root, agents, resolver(open_numbers), at(5000))


def test_stage_runs_group_by_issue_newest_issue_first() -> None:
    view = goal_sdlc_view()
    detail = view.detail()

    rows = items(detail["issues"])
    assert [row["issue"] for row in rows] == [406, 405]
    newest, older = rows
    assert newest["issue_url"] == "https://github.com/o/r/issues/406"
    (chip,) = items(newest["stages"])
    assert chip["stage"] == "submit-draft-pr" and chip["outcome"] == "stopped"
    assert chip["note"] == "duplicate evidence" and chip["subagents"] == 1
    assert [(c["stage"], c["outcome"]) for c in items(older["stages"])] == [
        ("next-ticket", "stopped"),
        ("next-ticket", "done"),
    ]
    assert older["outcome"] == "done" and older["duration_s"] == 390.0
    assert obj(detail["upkeep"]) == {"runs": 1, "running": 0, "duration_s": 60.0}


def test_attention_skips_superseded_stops_and_children_of_finished_stages() -> None:
    attention = goal_sdlc_view().attention()

    (item,) = attention
    assert item["agent_id"] == "c3" and item["kind"] == "stopped"
    assert item["issue"] == 406 and item["note"] == "duplicate evidence"


def test_attention_drops_a_stage_whose_issue_is_closed_since() -> None:
    assert goal_sdlc_view(open_numbers=[405]).attention() == []
    (item,) = goal_sdlc_view(open_numbers=[405, 406]).attention()
    assert item["agent_id"] == "c3"


def test_attention_outside_goal_sdlc_ends_at_the_next_request() -> None:
    root = Agent("s", "session")
    root.apply(human("start", 0))
    root.apply(tool_use("a1", "Agent", {"description": "x"}, 1))
    quiet = subagent("q", [human("Look into the slow build", 1)], "a1", finished=False)
    view = SessionView(root, [quiet], resolver(), at(1000))

    (item,) = view.attention()
    assert item["kind"] == "stalled" and item["title"] == "Look into the slow build"

    root.apply(human("never mind", 500))
    assert SessionView(root, [quiet], resolver(), at(1000)).attention() == []


def test_background_counts_only_the_problems_a_run_recovered_from() -> None:
    root = Agent("s", "session")
    root.apply(tool_use("t", "Bash", {"command": "x"}, 0))
    root.apply(tool_result("t", 1, "boom", is_error=True))
    root.apply({"type": "system", "hookErrors": ["blocked"], "timestamp": "x"})
    view = SessionView(root, [], resolver(), at(10), unreadable=2)

    assert view.background() == {
        "api_errors": 0,
        "hook_blocks": 1,
        "tool_errors": 1,
        "unreadable_records": 2,
    }


def test_turns_show_request_reply_tools_and_subagents() -> None:
    root = Agent("s", "session")
    root.apply(human("find the bug", 0))
    root.apply(tool_use("a1", "Agent", {"description": "search"}, 1))
    root.apply(tool_result("a1", 30))
    root.apply(text("Found it. It was a typo.", 31))
    root.apply(human("fix it", 40))
    root.apply(tool_use("t1", "Edit", {"file_path": "a.py"}, 41))
    helper = subagent("h", [human("search", 1), text("In a.py.", 29)], launch="a1")
    view = SessionView(root, [helper], resolver(), at(50))

    latest, first = view.turns()
    assert latest["request"] == "fix it" and latest["running"] is True
    assert latest["duration_s"] == 10.0 and latest["top_tools"] == [["Edit", 1]]
    assert first["running"] is False and first["duration_s"] == 31.0
    assert first["reply"] == "Found it."
    (child,) = items(first["subagents"])
    assert child["agent_id"] == "h" and child["outcome"] == "done"
    assert child["note"] == "In a.py."
    now = view.summary()["now"]
    assert obj(now)["request"] == "fix it" and obj(now)["doing"] == "Edit: a.py"


def test_live_lists_working_subagents_by_depth_with_their_open_step() -> None:
    root = Agent("s", "session")
    root.apply(tool_use("a1", "Agent", {"description": "outer"}, 0))
    outer = subagent(
        "o",
        [human("outer", 0), tool_use("a2", "Agent", {"description": "inner"}, 5)],
        launch="a1",
        finished=False,
    )
    inner = subagent(
        "i",
        [human("inner", 5), tool_use("t", "Bash", {"command": "make"}, 6)],
        launch="a2",
        finished=False,
    )
    view = SessionView(root, [inner, outer], resolver(), at(20))

    live = view.live()
    assert [(a["agent_id"], a["depth"]) for a in live] == [("o", 1), ("i", 2)]
    assert live[1]["doing"] == "Bash: make" and live[1]["running_s"] == 15.0
    summary = view.summary()
    assert summary["workflow"] is None and summary["now"] is not None
    assert view.agent_detail("o") is not None
    assert obj(view.agent_detail("o"))["doing"] == "Agent: inner"
