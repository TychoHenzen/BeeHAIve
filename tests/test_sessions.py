from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from beehaiive.records import Record
from beehaiive.repos import RepoResolver
from beehaiive.sessions import SessionIndex, SessionTracker
from beehaiive.view import STEP_LIMIT
from tests.transcripts import (
    append_jsonl,
    at,
    human,
    notification,
    text,
    tool_result,
    tool_use,
    write_jsonl,
)

SESSION = "11111111-aaaa"
PROJECTS = json.dumps(
    {
        "data": {
            "repository": {
                "projectsV2": {
                    "nodes": [{"number": 10, "title": "Board", "url": "https://p/10"}]
                }
            }
        }
    }
)


def obj(value: object) -> Record:
    assert isinstance(value, dict)
    return cast(Record, value)


def items(value: object) -> list[Record]:
    assert isinstance(value, list)
    return [obj(item) for item in cast(list[object], value)]


class Runner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(args[0])
        if args[0] == "git":
            return "https://github.com/o/r.git\n"
        return PROJECTS


class Clock:
    def __init__(self, seconds: float) -> None:
        self.now = at(seconds)

    def __call__(self) -> datetime:
        return self.now


class Tree:
    """A projects folder holding one session and its subagents."""

    def __init__(self, root: Path, session: str = SESSION) -> None:
        self.folder = root / "C--work-repo"
        self.main = self.folder / f"{session}.jsonl"
        self.subagents = self.folder / session / "subagents"

    def write_main(self, records: list[Record], mtime: float) -> None:
        write_jsonl(self.main, records)
        touch(self.main, mtime)

    def write_subagent(
        self, agent_id: str, records: list[Record], meta: Record | None = None
    ) -> Path:
        path = self.subagents / f"agent-{agent_id}.jsonl"
        write_jsonl(path, records)
        if meta is not None:
            meta_path = path.with_suffix(".meta.json")
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return path


def touch(path: Path, seconds: float) -> None:
    stamp = at(seconds).timestamp()
    os.utime(path, (stamp, stamp))


def make_index(root: Path, clock: Clock, runner: Runner | None = None) -> SessionIndex:
    return SessionIndex(
        root, timedelta(hours=1), resolver=RepoResolver(runner or Runner()), clock=clock
    )


def goal_sdlc_tree(root: Path) -> Tree:
    tree = Tree(root)
    tree.write_main(
        [
            human("/goal-sdlc", 0, uuid="p1"),
            tool_use("k1", "Skill", {"skill": "dod-guard:goal-sdlc"}, 1),
            tool_result("k1", 2),
            tool_use("a1", "Agent", {"description": "next-ticket 405"}, 3),
            tool_result("a1", 4, result={"status": "async_launched", "agentId": "c1"}),
            {"type": "custom-title", "customTitle": "Goal run"},
        ],
        mtime=4,
    )
    tree.write_subagent(
        "c1",
        [
            human("Skill: dod-guard:next-ticket 405\nImplement it.", 4),
            tool_use("a2", "Agent", {"description": "Write the tests"}, 10),
            tool_result("a2", 50),
            tool_use("t1", "Bash", {"command": "pytest"}, 60),
            {
                "type": "pr-link",
                "prNumber": 12,
                "prUrl": "https://github.com/o/r/pull/12",
                "prRepository": "o/r",
            },
        ],
        {
            "agentType": "stage-standard",
            "description": "next-ticket 405",
            "toolUseId": "a1",
        },
    )
    tree.write_subagent(
        "c2",
        [human("Write the tests for #405", 10), text("Tests written for #405", 49)],
        {"agentType": "tdd", "description": "Write the tests", "toolUseId": "a2"},
    )
    return tree


def test_goal_sdlc_session_links_stage_issue_project_and_subagents(
    tmp_path: Path,
) -> None:
    goal_sdlc_tree(tmp_path)
    index = make_index(tmp_path, Clock(70))

    assert index.list_sessions() == {"indexing": True, "sessions": []}
    index.refresh()
    listing = index.list_sessions()
    assert listing["indexing"] is False
    (summary,) = items(listing["sessions"])

    assert summary["title"] == "Goal run"
    assert summary["workflow"] == "goal-sdlc"
    assert summary["status"] == "working"
    assert summary["repository"] == {"name": "o/r", "url": "https://github.com/o/r"}
    assert summary["projects"] == [
        {"number": 10, "title": "Board", "url": "https://p/10"}
    ]
    assert items(summary["pull_requests"])[0]["number"] == 12
    (stage,) = items(obj(summary["now"])["stages"])
    assert stage["stage"] == "next-ticket" and stage["issue"] == 405
    assert stage["issue_url"] == "https://github.com/o/r/issues/405"
    assert stage["agent_id"] == "c1" and stage["outcome"] == "running"
    (live,) = items(summary["live"])
    assert live["agent_id"] == "c1" and live["doing"] == "Bash: pytest"
    assert summary["attention_count"] == 0

    detail = index.session_detail(SESSION)
    assert detail is not None
    (row,) = items(detail["issues"])
    assert row["issue"] == 405 and row["outcome"] == "running"
    assert [c["agent_id"] for c in items(row["stages"])] == ["c1"]
    assert obj(detail["upkeep"])["runs"] == 0
    assert "turns" not in detail


def test_agent_detail_has_result_children_and_steps(tmp_path: Path) -> None:
    goal_sdlc_tree(tmp_path)
    index = make_index(tmp_path, Clock(70))
    index.refresh()

    root = index.agent_detail(SESSION, SESSION)
    assert root is not None
    assert root["stage"] is None and root["outcome"] is None
    launch = next(s for s in items(root["steps"]) if s["id"] == "a1")
    assert launch["child_agent_id"] == "c1" and launch["status"] == "running"
    stage = index.agent_detail(SESSION, "c1")
    assert stage is not None
    assert stage["stage"] == "next-ticket" and stage["outcome"] == "running"
    assert stage["agent_type"] == "stage-standard"
    (child,) = items(stage["children"])
    assert child["agent_id"] == "c2" and child["outcome"] == "done"
    assert child["note"] == "Tests written for #405"
    leaf = index.agent_detail(SESSION, "c2")
    assert leaf is not None
    assert leaf["result"] == "Tests written for #405"
    assert leaf["problem_counts"] == {} and leaf["children"] == []
    assert index.agent_detail(SESSION, "missing") is None
    assert index.agent_detail("missing", SESSION) is None
    assert index.session_detail("missing") is None


def test_projects_are_fetched_only_for_goal_sdlc_sessions(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    tree.write_main(
        [human("hi", 0), tool_use("t1", "Bash", {"command": "ls"}, 1)], mtime=1
    )
    runner = Runner()
    index = make_index(tmp_path, Clock(10), runner)
    index.refresh()
    index.refresh()

    (summary,) = items(index.list_sessions()["sessions"])
    assert summary["workflow"] is None and summary["projects"] == []
    assert runner.calls == ["git"]
    detail = index.session_detail(SESSION)
    assert detail is not None
    assert "issues" not in detail
    (turn,) = items(detail["turns"])
    assert turn["request"] == "hi" and turn["running"] is True


def test_statuses_follow_notifications_and_time(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    tree.write_main(
        [
            tool_use("a1", "Agent", {"description": "done one"}, 0),
            tool_result("a1", 1, result={"status": "async_launched"}),
            tool_use("a2", "Agent", {"description": "failed one"}, 0),
            tool_result("a2", 1, result={"status": "async_launched"}),
            tool_use("a3", "Agent", {"description": "silent one"}, 0),
            tool_result("a3", 1, result={"status": "async_launched"}),
            notification("a1", 100, "completed"),
            notification("a2", 100, "failed", "crashed"),
        ],
        mtime=100,
    )
    for agent_id, tool_id in (("d", "a1"), ("f", "a2"), ("s", "a3")):
        tree.write_subagent(agent_id, [human("go", 1)], {"toolUseId": tool_id})
    tree.write_subagent("m", [human("go", 1)], {"finished": True})
    tree.write_subagent("o", [human("go", 1)], {"toolUseId": "unknown"})
    index = make_index(tmp_path, Clock(1000))
    index.refresh()

    status: dict[str, object] = {}
    for agent_id in (SESSION, "d", "f", "s", "m", "o"):
        agent = index.agent_detail(SESSION, agent_id)
        assert agent is not None
        status[agent_id] = agent["status"]
    assert status == {
        SESSION: "idle",
        "d": "finished",
        "f": "failed",
        "s": "stalled",
        "m": "finished",
        "o": "stalled",
    }
    detail = index.session_detail(SESSION)
    assert detail is not None
    attention = {a["agent_id"]: a["kind"] for a in items(detail["attention"])}
    assert attention == {"f": "failed", "s": "stalled", "o": "stalled"}
    failed = next(a for a in items(detail["attention"]) if a["agent_id"] == "f")
    assert failed["note"] == "crashed"


def test_a_long_running_tool_keeps_an_agent_working(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    tree.write_main([tool_use("t1", "Bash", {"command": "make"}, 0)], mtime=0)
    index = make_index(tmp_path, Clock(14 * 60))
    index.refresh()
    (summary,) = items(index.list_sessions()["sessions"])
    assert summary["status"] == "working"

    index.clock = Clock(16 * 60)
    (summary,) = items(index.list_sessions()["sessions"])
    assert summary["status"] == "idle"


def test_refresh_reads_appended_records_and_restarts_after_truncation(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path)
    tree.write_main([human("one", 0)], mtime=0)
    index = make_index(tmp_path, Clock(10))
    index.refresh()

    append_jsonl(tree.main, [human("two", 5)])
    touch(tree.main, 5)
    index.refresh()
    detail = index.agent_detail(SESSION, SESSION)
    assert detail is not None and detail["step_count"] == 2

    tree.write_main([human("fresh", 6)], mtime=6)
    index.refresh()
    detail = index.agent_detail(SESSION, SESSION)
    assert detail is not None
    assert [s["detail"] for s in items(detail["steps"])] == ["fresh"]


def test_new_subagents_appear_only_after_the_whole_pass(tmp_path: Path) -> None:
    goal_sdlc_tree(tmp_path)
    index = make_index(tmp_path, Clock(70))
    tracker = SessionTracker(SESSION, Tree(tmp_path).main)
    seen: list[int] = []

    class SpyLock:
        def __enter__(self) -> None:
            seen.append(len(tracker.subagents))

        def __exit__(self, *_: object) -> None:
            return None

    tracker.refresh(cast(threading.Lock, SpyLock()))
    assert seen == [0, 0] and set(tracker.subagents) == {"c1", "c2"}
    index.refresh()
    detail = index.session_detail(SESSION)
    assert detail is not None and obj(detail["upkeep"])["runs"] == 0


def test_subagent_truncation_keeps_its_meta(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    tree.write_main([human("go", 0)], mtime=0)
    path = tree.write_subagent(
        "c", [human("a", 0), human("b", 1)], {"agentType": "worker", "finished": True}
    )
    index = make_index(tmp_path, Clock(10))
    index.refresh()
    write_jsonl(path, [human("c", 2)])
    index.refresh()

    detail = index.agent_detail(SESSION, "c")
    assert detail is not None
    assert detail["agent_type"] == "worker" and detail["step_count"] == 1


def test_sessions_outside_the_lookback_are_skipped_and_dropped(tmp_path: Path) -> None:
    old = Tree(tmp_path, "old")
    old.write_main([human("old", 0)], mtime=0)
    new = Tree(tmp_path, "new")
    new.write_main([human("new", 3500)], mtime=3500)
    index = make_index(tmp_path, Clock(3590))
    index.refresh()
    assert [s["id"] for s in items(index.list_sessions()["sessions"])] == ["new", "old"]

    index.clock = Clock(3700)
    index.refresh()
    assert [s["id"] for s in items(index.list_sessions()["sessions"])] == ["new"]


def test_working_sessions_sort_first(tmp_path: Path) -> None:
    # "busy" is older but still inside a long tool call; "recent" has gone quiet.
    busy = [tool_use("t1", "Bash", {"command": "make"}, 3000)]
    Tree(tmp_path, "busy").write_main(busy, mtime=3000)
    Tree(tmp_path, "recent").write_main([human("x", 3400)], mtime=3400)
    index = make_index(tmp_path, Clock(3550))
    index.refresh()

    sessions = items(index.list_sessions()["sessions"])
    assert [(s["id"], s["status"]) for s in sessions] == [
        ("busy", "working"),
        ("recent", "idle"),
    ]


def test_titles_fall_back_to_folder_then_id(tmp_path: Path) -> None:
    Tree(tmp_path, "with-cwd").write_main([human("x", 0)], mtime=0)
    Tree(tmp_path, "no-cwd").write_main([{"type": "user", "message": {}}], mtime=0)
    index = make_index(tmp_path, Clock(10))
    index.refresh()

    titles = {s["id"]: s["title"] for s in items(index.list_sessions()["sessions"])}
    assert titles == {"with-cwd": "repo", "no-cwd": "no-cwd"}


def test_unreadable_records_and_step_limit(tmp_path: Path) -> None:
    tree = Tree(tmp_path)
    records: list[Record] = []
    for n in range(STEP_LIMIT + 5):
        records.append(tool_use(f"t{n}", "Bash", {"command": str(n)}, n / 100))
        records.append(tool_result(f"t{n}", n / 100))
    tree.write_main(records, mtime=10)
    with tree.main.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"user", broken\n')
    index = make_index(tmp_path, Clock(20))
    index.refresh()

    detail = index.agent_detail(SESSION, SESSION)
    assert detail is not None
    assert detail["step_count"] == STEP_LIMIT + 5
    assert len(items(detail["steps"])) == STEP_LIMIT
    session = index.session_detail(SESSION)
    assert session is not None
    assert obj(session["background"])["unreadable_records"] == 1


def test_background_thread_indexes_and_stops(tmp_path: Path) -> None:
    Tree(tmp_path).write_main([human("x", 0)], mtime=0)
    index = SessionIndex(
        tmp_path,
        timedelta(hours=1),
        resolver=RepoResolver(Runner()),
        clock=Clock(10),
        interval_seconds=0.01,
    )
    index.start()
    index.start()
    deadline = time.monotonic() + 5
    while not index.indexed_once and time.monotonic() < deadline:
        time.sleep(0.01)
    index.stop()

    assert index.indexed_once
    assert len(items(index.list_sessions()["sessions"])) == 1
    index.stop()


def test_stop_interrupts_a_refresh(tmp_path: Path) -> None:
    Tree(tmp_path).write_main([human("x", 0)], mtime=0)
    index = make_index(tmp_path, Clock(10))
    index.stop()
    index.refresh()

    assert not index.indexed_once
    assert index.list_sessions()["sessions"] == []
