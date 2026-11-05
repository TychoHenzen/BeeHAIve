"""Turn one session's parsed agents into what a person wants to see.

The unit is never a tool call. A goal-sdlc session is shown as stage runs
grouped by issue; any other session as the turns that answered each request.
Problems are reduced to the ones that need a person: a stage that stopped,
failed or went quiet and that nothing later took care of.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from .activity import Agent, Step, Turn
from .outcomes import first_sentence, issue_in, stage_name, stop_reason
from .repos import ProjectLink, RepoResolver, Repository

GOAL_SDLC = "dod-guard:goal-sdlc"
ACTIVE_WINDOW = timedelta(minutes=2)
# A long tool call (a test suite, a build) writes nothing until it ends.
OPEN_STEP_WINDOW = timedelta(minutes=15)
STEP_LIMIT = 400
TURN_LIMIT = 100
CHILD_LIMIT = 200
LIVE_LIMIT = 8
Json = dict[str, object]


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


class SessionView:
    """Derived facts about one session at one moment."""

    def __init__(
        self,
        root: Agent,
        subagents: list[Agent],
        resolver: RepoResolver,
        now: datetime,
        unreadable: int = 0,
    ) -> None:
        self.root = root
        self.agents = [root, *subagents]
        self.now = now
        self.unreadable = unreadable
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
        for agent in self.agents:
            self._compute_status(agent)
        self.is_goal_sdlc = GOAL_SDLC in root.skills_used
        self.repository: Repository | None = resolver.known_repository(root.cwd)
        self.projects: list[ProjectLink] = (
            resolver.known_projects(self.repository)
            if self.repository is not None and self.is_goal_sdlc
            else []
        )

    # Tree and status

    def _link_parents(self) -> dict[str, str]:
        owner: dict[str, Agent] = {}
        for agent in self.agents:
            for step in agent.steps:
                if step.kind == "agent":
                    owner[step.id] = agent
        parent: dict[str, str] = {}
        for agent in self.agents[1:]:
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
        elif agent.kind == "subagent" and ended:
            state = "finished"
        elif self._is_active(agent) or "working" in child_states:
            state = "working"
        else:
            state = "stalled" if agent.kind == "subagent" else "idle"
        self.status[agent.id] = state
        return state

    def _launch_step(self, agent: Agent) -> Step | None:
        parent = self.by_id.get(self.parent.get(agent.id, ""))
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

    def _working(self, agent: Agent) -> bool:
        return self.status.get(agent.id) == "working"

    def sort_key(self) -> tuple[int, datetime]:
        return (
            1 if self._working(self.root) else 0,
            self.root.last_activity or self.now,
        )

    # Names and links

    def title(self) -> str:
        root = self.root
        return root.title or (Path(root.cwd).name if root.cwd else root.id)

    def _issue_url(self, issue: int | None) -> str | None:
        if issue is None or self.repository is None:
            return None
        # GitHub redirects /issues/N to /pull/N when N is a pull request.
        return self.repository.issue_url(issue)

    def _issue(self, agent: Agent) -> int | None:
        if agent.label is not None and agent.label.issue is not None:
            return agent.label.issue
        return issue_in(agent.description)

    def _stage(self, agent: Agent) -> str:
        if agent.label is not None:
            return agent.label.stage
        text = agent.description or agent.title or agent.agent_type or "subagent"
        return stage_name(text, self._issue(agent))

    def _name(self, agent: Agent) -> str:
        if agent is self.root:
            return self.title()
        return agent.description or agent.title or agent.agent_type or agent.id

    def _end(self, agent: Agent) -> datetime | None:
        return self.now if self._working(agent) else agent.last_activity

    # Outcomes

    def _outcome(self, agent: Agent) -> str:
        status = self.status.get(agent.id, "idle")
        if status == "working":
            return "running"
        if status in ("failed", "stalled"):
            return status
        if stop_reason(agent.last_message):
            return "stopped"
        return "done"

    def _note(self, agent: Agent) -> str:
        outcome = self._outcome(agent)
        if outcome == "failed":
            launch = self._launch_step(agent)
            if launch is not None and launch.error:
                return first_sentence(launch.error)
        if outcome == "stopped":
            return stop_reason(agent.last_message) or ""
        return first_sentence(agent.last_message)

    def _stage_chip(self, agent: Agent) -> Json:
        issue = self._issue(agent)
        return {
            "agent_id": agent.id,
            "stage": self._stage(agent),
            "issue": issue,
            "issue_url": self._issue_url(issue),
            "started_at": iso(agent.started_at),
            "duration_s": seconds(agent.started_at, self._end(agent)),
            "outcome": self._outcome(agent),
            "note": self._note(agent),
            "subagents": len(self.children.get(agent.id, [])),
        }

    # goal-sdlc: stage runs grouped by issue

    def _dispatches(self) -> list[Agent]:
        return self.children.get(self.root.id, [])

    def _superseded(self, agent: Agent) -> bool:
        """A later stage ran for the same issue, so this one was dealt with."""
        issue = self._issue(agent)
        if issue is None or agent.started_at is None:
            return False
        return any(
            other is not agent
            and self._issue(other) == issue
            and other.started_at is not None
            and other.started_at > agent.started_at
            for other in self._dispatches()
        )

    def issues(self) -> tuple[list[Json], Json]:
        rows: dict[int, list[Agent]] = {}
        upkeep: list[Agent] = []
        for agent in self._dispatches():
            issue = self._issue(agent)
            if issue is None:
                upkeep.append(agent)
            else:
                rows.setdefault(issue, []).append(agent)
        views: list[tuple[datetime, Json]] = []
        for issue, stages in rows.items():
            first, last = stages[0], stages[-1]
            running = any(self._working(stage) for stage in stages)
            end = max((self._end(s) or self.now for s in stages), default=self.now)
            views.append(
                (
                    last.started_at or self.now,
                    {
                        "issue": issue,
                        "issue_url": self._issue_url(issue),
                        "started_at": iso(first.started_at),
                        "duration_s": seconds(first.started_at, end),
                        "outcome": "running" if running else self._outcome(last),
                        "stages": [self._stage_chip(stage) for stage in stages],
                    },
                )
            )
        views.sort(key=lambda pair: pair[0], reverse=True)
        upkeep_view: Json = {
            "runs": len(upkeep),
            "running": sum(1 for agent in upkeep if self._working(agent)),
            "duration_s": sum(
                seconds(a.started_at, self._end(a)) or 0.0 for a in upkeep
            ),
        }
        return [view for _, view in views], upkeep_view

    # Problems that need a person

    def attention(self) -> list[Json]:
        items: list[tuple[datetime, Json]] = []
        for agent in self.agents[1:]:
            outcome = self._outcome(agent)
            dispatched_by_root = self.parent.get(agent.id) == self.root.id
            if outcome not in ("failed", "stalled") and not (
                outcome == "stopped" and dispatched_by_root and self.is_goal_sdlc
            ):
                continue
            # A subagent of a parent that is still working, or of the session
            # itself, is nobody else's job; a finished parent dealt with it.
            parent = self.by_id.get(self.parent.get(agent.id, ""))
            if (
                parent is not None
                and parent is not self.root
                and not self._working(parent)
            ):
                continue
            if dispatched_by_root and self.is_goal_sdlc and self._superseded(agent):
                continue
            issue = self._issue(agent)
            items.append(
                (
                    agent.last_activity or self.now,
                    {
                        "kind": outcome,
                        "agent_id": agent.id,
                        "stage": self._stage(agent),
                        "title": self._name(agent),
                        "issue": issue,
                        "issue_url": self._issue_url(issue),
                        "at": iso(agent.last_activity),
                        "note": self._note(agent),
                    },
                )
            )
        items.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in items]

    def background(self) -> Json:
        counts = {"api_error": 0, "hook": 0, "tool_error": 0}
        for agent in self.agents:
            for problem in agent.problems:
                if problem.kind in counts:
                    counts[problem.kind] += 1
        return {
            "api_errors": counts["api_error"],
            "hook_blocks": counts["hook"],
            "tool_errors": counts["tool_error"],
            "unreadable_records": self.unreadable,
        }

    # What is happening now

    def _doing(self, agent: Agent) -> str | None:
        open_steps = [s for s in agent.open_steps() if s.kind != "prompt"]
        if not open_steps:
            return None
        step = max(open_steps, key=lambda s: s.started_at or self.now)
        return f"{step.name}: {step.detail}"

    def _depth(self, agent: Agent) -> int:
        depth, current = 0, agent.id
        while current in self.parent and depth < 20:
            current = self.parent[current]
            depth += 1
        return depth

    def live(self) -> list[Json]:
        working = [a for a in self.agents[1:] if self._working(a)]
        working.sort(key=lambda a: (self._depth(a), a.started_at or self.now))
        return [
            {
                "agent_id": agent.id,
                "title": self._name(agent),
                "agent_type": agent.agent_type or None,
                "depth": self._depth(agent),
                "running_s": seconds(agent.started_at, self.now),
                "doing": self._doing(agent),
            }
            for agent in working
        ]

    def _now(self) -> Json | None:
        if not self._working(self.root):
            return None
        stages = [
            self._stage_chip(agent)
            for agent in self._dispatches()
            if self.is_goal_sdlc and self._working(agent)
        ]
        turn = self.root.turns[-1] if self.root.turns else None
        return {
            "stages": stages,
            "request": turn.request if turn else "",
            "running_s": seconds(turn.started_at if turn else None, self.now),
            "doing": self._doing(self.root),
        }

    # Turns, for sessions that do not run goal-sdlc

    def _turn_view(self, turn: Turn, running: bool) -> Json:
        subagents: list[Json] = []
        for step_id in turn.agent_step_ids:
            step = self.root.step(step_id)
            child = self.by_id.get(step.child_agent_id or "") if step else None
            if child is not None:
                subagents.append(self._child_view(child))
        tools = sorted(turn.tool_counts.items(), key=lambda item: -item[1])
        return {
            "id": turn.id,
            "request": turn.request,
            "started_at": iso(turn.started_at),
            "duration_s": seconds(
                turn.started_at, self.now if running else turn.ended_at
            ),
            "running": running,
            "reply": first_sentence(turn.reply, 240),
            "tool_calls": sum(turn.tool_counts.values()),
            "top_tools": [[name, count] for name, count in tools[:4]],
            "errors": turn.errors,
            "subagents": subagents,
        }

    def turns(self) -> list[Json]:
        turns = self.root.turns[-TURN_LIMIT:]
        working = self._working(self.root)
        last = len(turns) - 1
        return [
            self._turn_view(turn, working and i == last)
            for i, turn in reversed(list(enumerate(turns)))
        ]

    def _child_view(self, agent: Agent) -> Json:
        return {
            "agent_id": agent.id,
            "title": self._name(agent),
            "agent_type": agent.agent_type or None,
            "outcome": self._outcome(agent),
            "duration_s": seconds(agent.started_at, self._end(agent)),
            "note": self._note(agent),
            "subagents": len(self.children.get(agent.id, [])),
        }

    # API shapes

    def summary(self) -> Json:
        root = self.root
        prs = {pr.url: pr for agent in self.agents for pr in agent.pull_requests}
        live = self.live()
        return {
            "id": root.id,
            "title": self.title(),
            "cwd": root.cwd,
            "branch": root.branch,
            "status": self.status.get(root.id),
            "workflow": "goal-sdlc" if self.is_goal_sdlc else None,
            "started_at": iso(root.started_at),
            "last_activity": iso(root.last_activity),
            "duration_s": seconds(root.started_at, root.last_activity),
            "now": self._now(),
            "live": live[:LIVE_LIMIT],
            "live_more": max(0, len(live) - LIVE_LIMIT),
            "attention_count": len(self.attention()),
            "background": self.background(),
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
                {"number": pr.number, "url": pr.url} for pr in prs.values()
            ],
            "tokens": {
                "input": sum(a.input_tokens for a in self.agents),
                "output": sum(a.output_tokens for a in self.agents),
            },
        }

    def detail(self) -> Json:
        view: Json = {**self.summary(), "attention": self.attention()}
        if self.is_goal_sdlc:
            issues, upkeep = self.issues()
            view["issues"] = issues
            view["upkeep"] = upkeep
        else:
            view["turns"] = self.turns()
        return view

    def agent_detail(self, agent_id: str) -> Json | None:
        agent = self.by_id.get(agent_id)
        if agent is None:
            return None
        tools: dict[str, int] = {}
        for step in agent.steps:
            if step.kind != "prompt":
                tools[step.name] = tools.get(step.name, 0) + 1
        problems: dict[str, int] = {}
        for problem in agent.problems:
            problems[problem.kind] = problems.get(problem.kind, 0) + 1
        children = self.children.get(agent.id, [])
        issue = self._issue(agent)
        return {
            "id": agent.id,
            "kind": agent.kind,
            "title": self._name(agent),
            "stage": self._stage(agent) if agent is not self.root else None,
            "issue": issue,
            "issue_url": self._issue_url(issue),
            "agent_type": agent.agent_type or None,
            "model": agent.model,
            "status": self.status.get(agent.id),
            "outcome": self._outcome(agent) if agent is not self.root else None,
            "started_at": iso(agent.started_at),
            "last_activity": iso(agent.last_activity),
            "duration_s": seconds(agent.started_at, self._end(agent)),
            "tokens": {"input": agent.input_tokens, "output": agent.output_tokens},
            "doing": self._doing(agent),
            "result": agent.last_message,
            "stop_reason": stop_reason(agent.last_message),
            "children": [self._child_view(c) for c in children[-CHILD_LIMIT:]],
            "children_total": len(children),
            "tool_summary": sorted(tools.items(), key=lambda item: -item[1]),
            "problem_counts": problems,
            "steps": [_step_view(step, self.now) for step in agent.steps[-STEP_LIMIT:]],
            "step_count": len(agent.steps),
        }


def _step_view(step: Step, now: datetime) -> Json:
    end = step.ended_at or (now if step.status == "running" else None)
    return {
        "id": step.id,
        "kind": step.kind,
        "name": step.name,
        "detail": step.detail,
        "status": step.status,
        "started_at": iso(step.started_at),
        "duration_s": seconds(step.started_at, end),
        "error": step.error,
        "child_agent_id": step.child_agent_id,
    }
