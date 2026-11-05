"""Index Claude Code sessions on disk and project them for the dashboard.

Layout that Claude Code writes, which this module reads and never changes:

    <projects>/<project-slug>/<session-id>.jsonl                 main thread
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.jsonl
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.meta.json
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .activity import RECORD_MARKERS, Agent, Problem, Step
from .records import TranscriptTail, get_str, read_json_file
from .repos import ProjectLink, RepoResolver, Repository

GOAL_SDLC = "dod-guard:goal-sdlc"
ACTIVE_WINDOW = timedelta(minutes=2)
# A long tool call (a test suite, a build) writes nothing until it ends.
OPEN_STEP_WINDOW = timedelta(minutes=15)
STEP_LIMIT = 400

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


@dataclass
class _AgentFile:
    agent: Agent
    tail: TranscriptTail
    meta_path: Path | None = None
    meta_loaded: bool = False

    def refresh(self) -> None:
        records = self.tail.read_new()
        if self.tail.was_reset:
            fresh = Agent(self.agent.id, self.agent.kind)
            fresh.agent_type = self.agent.agent_type
            fresh.description = self.agent.description
            fresh.parent_tool_use_id = self.agent.parent_tool_use_id
            self.agent = fresh
        for record in records:
            self.agent.apply(record)
        if self.meta_path is not None and not self.agent.finished:
            self._load_meta(self.meta_path)

    def _load_meta(self, path: Path) -> None:
        meta = read_json_file(path)
        if meta is None:
            return
        self.agent.agent_type = get_str(meta, "agentType") or self.agent.agent_type
        self.agent.description = get_str(meta, "description") or self.agent.description
        self.agent.parent_tool_use_id = (
            get_str(meta, "toolUseId") or self.agent.parent_tool_use_id
        )
        self.agent.finished = meta.get("finished") is True
        self.meta_loaded = True


@dataclass
class SessionTracker:
    session_id: str
    main_path: Path
    main: _AgentFile = field(init=False)
    subagents: dict[str, _AgentFile] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        self.main = _AgentFile(
            Agent(self.session_id, "session"),
            TranscriptTail(self.main_path, RECORD_MARKERS),
        )

    @property
    def subagent_dir(self) -> Path:
        return self.main_path.with_suffix("") / "subagents"

    def refresh(self, lock: threading.Lock) -> None:
        """Read new records, taking the lock per file so readers never wait long."""
        with lock:
            self.main.refresh()
        directory = self.subagent_dir
        if not directory.is_dir():
            return
        for path in directory.glob("agent-*.jsonl"):
            agent_id = path.stem.removeprefix("agent-")
            with lock:
                tracked = self.subagents.get(agent_id)
                if tracked is None:
                    tracked = _AgentFile(
                        Agent(agent_id, "subagent"),
                        TranscriptTail(path, RECORD_MARKERS),
                        path.with_suffix(".meta.json"),
                    )
                    self.subagents[agent_id] = tracked
                tracked.refresh()

    def agents(self) -> list[Agent]:
        return [self.main.agent, *(t.agent for t in self.subagents.values())]

    def unreadable(self) -> int:
        return self.main.tail.unreadable + sum(
            t.tail.unreadable for t in self.subagents.values()
        )


class SessionIndex:
    """Keep every recently active session parsed, reading only new bytes.

    A background thread does the parsing (start/stop), so a request never
    waits for a first read of a large transcript; it sees what is indexed so far.
    """

    def __init__(
        self,
        projects_dir: Path,
        lookback: timedelta,
        resolver: RepoResolver | None = None,
        clock: Clock = utc_now,
        interval_seconds: float = 2.0,
    ) -> None:
        self.projects_dir = projects_dir
        self.lookback = lookback
        self.resolver = resolver or RepoResolver()
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.indexed_once = False
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionTracker] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="beehaiive-index", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval_seconds)

    def _recent_transcripts(self) -> list[Path]:
        cutoff = (self.clock() - self.lookback).timestamp()
        found: list[tuple[float, Path]] = []
        for path in self.projects_dir.glob("*/*.jsonl"):
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified >= cutoff:
                found.append((modified, path))
        # Newest first, so the session in use appears before old long runs.
        found.sort(reverse=True)
        return [path for _, path in found]

    def refresh(self) -> None:
        paths = self._recent_transcripts()
        seen = {path.stem for path in paths}
        for path in paths:
            if self._stop.is_set():
                return
            with self._lock:
                tracker = self._sessions.get(path.stem)
                if tracker is None:
                    tracker = SessionTracker(path.stem, path)
                    self._sessions[path.stem] = tracker
            tracker.refresh(self._lock)
            self._resolve_links(tracker)
        with self._lock:
            for session_id in list(self._sessions):
                if session_id not in seen:
                    del self._sessions[session_id]
            self.indexed_once = True

    def _resolve_links(self, tracker: SessionTracker) -> None:
        # Runs git and gh outside the index lock; views read only the cache.
        # Only this thread changes the agents, so reading them unlocked is safe.
        root = tracker.main.agent
        repository = self.resolver.repository(root.cwd)
        if repository is not None and GOAL_SDLC in root.skills_used:
            self.resolver.projects(repository)

    def list_sessions(self) -> dict[str, object]:
        with self._lock:
            now = self.clock()
            views = [
                _SessionView(t, self.resolver, now) for t in self._sessions.values()
            ]
            views.sort(key=lambda view: view.sort_key(), reverse=True)
            return {
                "indexing": not self.indexed_once,
                "sessions": [view.summary() for view in views],
            }

    def _view(self, session_id: str) -> _SessionView | None:
        tracker = self._sessions.get(session_id)
        if tracker is None:
            return None
        return _SessionView(tracker, self.resolver, self.clock())

    def session_detail(self, session_id: str) -> dict[str, object] | None:
        with self._lock:
            view = self._view(session_id)
            return view.detail() if view else None

    def agent_detail(self, session_id: str, agent_id: str) -> dict[str, object] | None:
        with self._lock:
            view = self._view(session_id)
            return view.agent_detail(agent_id) if view else None


class _SessionView:
    """Derived facts about one session at one moment: tree, status, links."""

    def __init__(self, tracker: SessionTracker, resolver: RepoResolver, now: datetime):
        self.tracker = tracker
        self.now = now
        self.agents = tracker.agents()
        self.root = tracker.main.agent
        self.by_id = {agent.id: agent for agent in self.agents}
        self.parent = self._link_parents()
        self.children: dict[str, list[Agent]] = {}
        for agent in self.agents:
            parent_id = self.parent.get(agent.id)
            if parent_id is not None:
                self.children.setdefault(parent_id, []).append(agent)
        for siblings in self.children.values():
            siblings.sort(key=lambda a: a.started_at or now)
        self.status: dict[str, str] = {}
        self._compute_status(self.root)
        for agent in self.agents:
            if agent.id not in self.status:
                self._compute_status(agent)
        self.is_goal_sdlc = GOAL_SDLC in self.root.skills_used
        self.repository: Repository | None = resolver.known_repository(self.root.cwd)
        self.projects: list[ProjectLink] = (
            resolver.known_projects(self.repository)
            if self.repository is not None and self.is_goal_sdlc
            else []
        )

    def _link_parents(self) -> dict[str, str]:
        owner: dict[str, Agent] = {}
        for agent in self.agents:
            for step in agent.steps:
                if step.kind == "agent":
                    owner[step.id] = agent
        parent: dict[str, str] = {}
        for agent in self.agents:
            if agent is self.root:
                continue
            launcher = owner.get(agent.parent_tool_use_id or "")
            parent[agent.id] = launcher.id if launcher else self.root.id
            if launcher is not None:
                step = launcher.step(agent.parent_tool_use_id or "")
                if step is not None:
                    step.child_agent_id = agent.id
        return parent

    def _compute_status(self, agent: Agent) -> str:
        cached = self.status.get(agent.id)
        if cached is not None:
            return cached
        self.status[agent.id] = "idle"  # guards against a cycle in bad data
        child_states = [
            self._compute_status(c) for c in self.children.get(agent.id, [])
        ]
        launch = self._launch_step(agent)
        ended = agent.finished or (launch is not None and launch.status == "done")
        if launch is not None and launch.status == "failed":
            state = "failed"
        elif agent.kind == "subagent" and ended and not agent.open_steps():
            state = "finished"
        elif self._is_active(agent) or "working" in child_states:
            state = "working"
        else:
            state = "stalled" if agent.kind == "subagent" else "idle"
        self.status[agent.id] = state
        return state

    def _launch_step(self, agent: Agent) -> Step | None:
        parent_id = self.parent.get(agent.id)
        parent = self.by_id.get(parent_id) if parent_id else None
        if parent is None or not agent.parent_tool_use_id:
            return None
        return parent.step(agent.parent_tool_use_id)

    def _is_active(self, agent: Agent) -> bool:
        if agent.last_activity and self.now - agent.last_activity < ACTIVE_WINDOW:
            return True
        return any(
            step.kind == "tool"
            and step.started_at is not None
            and self.now - step.started_at < OPEN_STEP_WINDOW
            for step in agent.open_steps()
        )

    def sort_key(self) -> tuple[int, datetime]:
        working = self.status.get(self.root.id) == "working"
        return (1 if working else 0, self.root.last_activity or self.now)

    def _current(self, agent: Agent) -> Step | None:
        open_steps = [s for s in agent.open_steps() if s.kind != "prompt"]
        if open_steps:
            return max(open_steps, key=lambda s: s.started_at or self.now)
        return agent.steps[-1] if agent.steps else None

    def _depth(self, agent: Agent) -> int:
        depth = 0
        current = agent.id
        while current in self.parent and depth < 20:
            current = self.parent[current]
            depth += 1
        return depth

    def _issue_url(self, issue: int | None) -> str | None:
        if issue is None or self.repository is None:
            return None
        return self.repository.issue_url(issue)

    def _stage(self) -> dict[str, object] | None:
        """The goal-sdlc stage in flight: the newest working labelled subagent."""
        labelled = [
            a
            for a in self.agents
            if a.label is not None and self.parent.get(a.id) == self.root.id
        ]
        if not labelled:
            return None
        working = [a for a in labelled if self.status.get(a.id) == "working"]
        pool = working or labelled
        agent = max(pool, key=lambda a: a.started_at or self.now)
        label = agent.label
        assert label is not None
        return {
            "stage": label.stage,
            "skill": label.skill,
            "args": label.args,
            "issue": label.issue,
            "issue_url": self._issue_url(label.issue),
            "agent_id": agent.id,
            "status": self.status.get(agent.id),
        }

    def _title(self) -> str:
        root = self.root
        return root.title or (Path(root.cwd).name if root.cwd else root.id)

    def summary(self) -> dict[str, object]:
        root = self.root
        problems = sum(len(a.problems) for a in self.agents)
        working = sum(1 for a in self.agents if self.status.get(a.id) == "working")
        pull_requests = [pr for a in self.agents for pr in a.pull_requests]
        unique_prs = {pr.url: pr for pr in pull_requests}
        current = self._current(root)
        return {
            "id": root.id,
            "title": self._title(),
            "cwd": root.cwd,
            "branch": root.branch,
            "model": root.model,
            "status": self.status.get(root.id),
            "workflow": "goal-sdlc" if self.is_goal_sdlc else None,
            "started_at": _iso(root.started_at),
            "last_activity": _iso(root.last_activity),
            "duration_s": _seconds(root.started_at, root.last_activity),
            "current": _step_view(current, self.now) if current else None,
            "stage": self._stage() if self.is_goal_sdlc else None,
            "repository": (
                {"name": self.repository.full_name, "url": self.repository.url}
                if self.repository
                else None
            ),
            "projects": [
                {"number": p.number, "title": p.title, "url": p.url}
                for p in self.projects
            ],
            "pull_requests": [
                {"number": pr.number, "url": pr.url, "repository": pr.repository}
                for pr in unique_prs.values()
            ],
            "counts": {
                "agents": len(self.agents),
                "working_agents": working,
                "problems": problems,
                "steps": sum(len(a.steps) for a in self.agents),
                "unreadable_records": self.tracker.unreadable(),
            },
            "tokens": {
                "input": sum(a.input_tokens for a in self.agents),
                "output": sum(a.output_tokens for a in self.agents),
            },
        }

    def detail(self) -> dict[str, object]:
        ordered: list[Agent] = []
        self._walk(self.root, ordered, set())
        return {
            **self.summary(),
            "agents": [self._agent_view(agent) for agent in ordered],
        }

    def agent_detail(self, agent_id: str) -> dict[str, object] | None:
        agent = self.by_id.get(agent_id)
        if agent is None:
            return None
        return {
            **self._agent_view(agent),
            "last_message": agent.last_message,
            "steps": [_step_view(step, self.now) for step in agent.steps[-STEP_LIMIT:]],
            "problems": [_problem_view(problem) for problem in agent.problems],
        }

    def _walk(self, agent: Agent, out: list[Agent], seen: set[str]) -> None:
        if agent.id in seen:
            return
        seen.add(agent.id)
        out.append(agent)
        for child in self.children.get(agent.id, []):
            self._walk(child, out, seen)

    def _agent_view(self, agent: Agent) -> dict[str, object]:
        current = self._current(agent)
        label = agent.label
        title = (
            self._title() if agent is self.root else agent.description or agent.title
        )
        return {
            "id": agent.id,
            "kind": agent.kind,
            "parent_id": self.parent.get(agent.id),
            "depth": self._depth(agent),
            "title": title,
            "agent_type": agent.agent_type or None,
            "model": agent.model,
            "status": self.status.get(agent.id),
            "label": (
                {
                    "stage": label.stage,
                    "skill": label.skill,
                    "args": label.args,
                    "issue": label.issue,
                    "issue_url": self._issue_url(label.issue),
                }
                if label
                else None
            ),
            "started_at": _iso(agent.started_at),
            "last_activity": _iso(agent.last_activity),
            "duration_s": _seconds(agent.started_at, agent.last_activity),
            "tokens": {"input": agent.input_tokens, "output": agent.output_tokens},
            "current": _step_view(current, self.now) if current else None,
            "step_count": len(agent.steps),
            "problem_count": len(agent.problems),
        }


def _step_view(step: Step, now: datetime) -> dict[str, object]:
    end = step.ended_at or (now if step.status == "running" else None)
    return {
        "id": step.id,
        "kind": step.kind,
        "name": step.name,
        "detail": step.detail,
        "status": step.status,
        "started_at": _iso(step.started_at),
        "ended_at": _iso(step.ended_at),
        "duration_s": _seconds(step.started_at, end),
        "error": step.error,
        "child_agent_id": step.child_agent_id,
    }


def _problem_view(problem: Problem) -> dict[str, object]:
    return {
        "at": _iso(problem.at),
        "kind": problem.kind,
        "message": problem.message,
        "step_id": problem.step_id,
    }
