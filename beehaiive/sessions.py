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

from .activity import RECORD_MARKERS, Agent
from .records import TranscriptTail, get_str, read_json_file
from .repos import RepoResolver
from .view import GOAL_SDLC, SessionView

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


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
        # New files are shown only after the whole pass, so a reader never
        # sees a subagent whose launching parent has not been read yet.
        found: dict[str, _AgentFile] = {}
        for path in directory.glob("agent-*.jsonl"):
            agent_id = path.stem.removeprefix("agent-")
            tracked = self.subagents.get(agent_id)
            if tracked is None:
                tracked = _AgentFile(
                    Agent(agent_id, "subagent"),
                    TranscriptTail(path, RECORD_MARKERS),
                    path.with_suffix(".meta.json"),
                )
                tracked.refresh()
                found[agent_id] = tracked
                continue
            with lock:
                tracked.refresh()
        with lock:
            self.subagents.update(found)

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
            views = [self._make_view(t, now) for t in self._sessions.values()]
            views.sort(key=lambda view: view.sort_key(), reverse=True)
            return {
                "indexing": not self.indexed_once,
                "sessions": [view.summary() for view in views],
            }

    def _make_view(self, tracker: SessionTracker, now: datetime) -> SessionView:
        agents = tracker.agents()
        return SessionView(
            agents[0], agents[1:], self.resolver, now, tracker.unreadable()
        )

    def _view(self, session_id: str) -> SessionView | None:
        tracker = self._sessions.get(session_id)
        return self._make_view(tracker, self.clock()) if tracker else None

    def session_detail(self, session_id: str) -> dict[str, object] | None:
        with self._lock:
            view = self._view(session_id)
            return view.detail() if view else None

    def agent_detail(self, session_id: str, agent_id: str) -> dict[str, object] | None:
        with self._lock:
            view = self._view(session_id)
            return view.agent_detail(agent_id) if view else None
