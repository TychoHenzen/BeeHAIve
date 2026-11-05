"""Turn transcript records into one agent's steps, problems and token use."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .outcomes import issue_in
from .records import (
    Record,
    as_dict,
    as_list,
    get_int,
    get_str,
    parse_time,
    text_of,
    type_markers,
)

# The record types Agent.apply reads; TranscriptTail skips every other line.
RECORD_MARKERS = type_markers(
    "assistant",
    "user",
    "system",
    "custom-title",
    "agent-name",
    "pr-link",
    "attachment",
)
AGENT_TOOLS = frozenset({"Agent", "Task"})
SNIPPET = 240

_NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_SKILL_LINE = re.compile(r"^\s*Skill:\s*(?P<skill>[\w.:-]+)(?P<args>[^\n]*)")


def _tag(body: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
    return match.group(1).strip() if match else None


def _snippet(text: str, limit: int = SNIPPET) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def summarize_input(name: str, tool_input: Record | None) -> str:
    """Pick the one input field that says what a tool call is doing."""
    for key in ("description", "file_path", "pattern", "skill", "url", "query"):
        value = get_str(tool_input, key)
        if value:
            if key == "skill":
                value = f"{value} {get_str(tool_input, 'args') or ''}".strip()
            return _snippet(value)
    command = get_str(tool_input, "command")
    if command:
        return _snippet(command)
    if tool_input:
        for value in tool_input.values():
            if isinstance(value, str) and value:
                return _snippet(value)
    return name


@dataclass(frozen=True)
class SkillLabel:
    """A dispatch that names its skill on the first line, as /goal-sdlc does."""

    skill: str
    args: str
    issue: int | None

    @property
    def stage(self) -> str:
        return self.skill.split(":")[-1]


def parse_skill_label(prompt: str) -> SkillLabel | None:
    match = _SKILL_LINE.match(prompt)
    if not match:
        return None
    args = match.group("args").strip()
    return SkillLabel(skill=match.group("skill"), args=args, issue=issue_in(args))


@dataclass
class Step:
    id: str
    kind: str  # "prompt", "tool" or "agent"
    name: str
    detail: str
    started_at: datetime | None
    ended_at: datetime | None = None
    status: str = "running"  # "running", "done" or "failed"
    error: str | None = None
    child_agent_id: str | None = None


@dataclass
class Turn:
    """One request to an agent and the work that answered it."""

    id: str
    request: str
    started_at: datetime | None
    ended_at: datetime | None = None
    reply: str = ""
    tool_counts: dict[str, int] = field(default_factory=lambda: {})
    agent_step_ids: list[str] = field(default_factory=lambda: [])
    errors: int = 0


@dataclass
class Problem:
    at: datetime | None
    kind: str  # "tool_error", "api_error", "hook", "agent_failed"
    message: str
    step_id: str | None = None


@dataclass
class PullRequest:
    number: int
    url: str
    repository: str


@dataclass
class Agent:
    """One thread of work: a whole session, or a subagent it dispatched."""

    id: str
    kind: str  # "session" or "subagent"
    title: str = ""
    agent_type: str = ""
    description: str = ""
    parent_tool_use_id: str | None = None
    label: SkillLabel | None = None
    cwd: str | None = None
    branch: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    last_activity: datetime | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    last_message: str = ""
    finished: bool = False
    steps: list[Step] = field(default_factory=lambda: [])
    turns: list[Turn] = field(default_factory=lambda: [])
    problems: list[Problem] = field(default_factory=lambda: [])
    pull_requests: list[PullRequest] = field(default_factory=lambda: [])
    skills_used: set[str] = field(default_factory=lambda: set())
    _open: dict[str, Step] = field(default_factory=lambda: {})
    _message_usage: dict[str, tuple[int, int]] = field(default_factory=lambda: {})

    def open_steps(self) -> list[Step]:
        return list(self._open.values())

    def _turn(self, at: datetime | None) -> Turn:
        # Work recorded before any human prompt still belongs to a turn.
        if not self.turns:
            self.turns.append(Turn(f"turn-{self.id}", "", at))
        return self.turns[-1]

    def step(self, step_id: str) -> Step | None:
        return self._open.get(step_id) or next(
            (step for step in reversed(self.steps) if step.id == step_id), None
        )

    def apply(self, record: Record) -> None:
        kind = get_str(record, "type")
        at = parse_time(get_str(record, "timestamp"))
        if at is not None:
            self.started_at = self.started_at or at
            self.last_activity = (
                at if self.last_activity is None else max(self.last_activity, at)
            )
        self.cwd = get_str(record, "cwd") or self.cwd
        self.branch = get_str(record, "gitBranch") or self.branch
        if kind == "assistant":
            self._apply_assistant(record, at)
        elif kind == "user":
            self._apply_user(record, at)
        elif kind == "system":
            self._apply_system(record, at)
        elif kind == "custom-title":
            self.title = get_str(record, "customTitle") or self.title
        elif kind == "agent-name" and not self.title:
            self.title = get_str(record, "agentName") or self.title
        elif kind == "pr-link":
            self._apply_pr_link(record)
        elif kind == "attachment":
            self._apply_attachment(record, at)
        # After dispatch, so a new prompt does not stretch the turn before it.
        if at is not None and self.turns and kind in ("assistant", "user"):
            self.turns[-1].ended_at = at

    def _apply_assistant(self, record: Record, at: datetime | None) -> None:
        message = as_dict(record.get("message"))
        self.model = get_str(message, "model") or self.model
        self._count_usage(message)
        if record.get("isApiErrorMessage") is True:
            text = text_of(message.get("content") if message else None)
            self.problems.append(
                Problem(at, "api_error", _snippet(text or "API error"))
            )
            return
        for item in as_list(message.get("content") if message else None):
            block = as_dict(item)
            block_type = get_str(block, "type")
            if block_type == "text":
                text = get_str(block, "text") or ""
                if text.strip():
                    self.last_message = _snippet(text, 1200)
                    self._turn(at).reply = self.last_message
            elif block_type == "tool_use":
                self._open_tool(block, at)

    def _count_usage(self, message: Record | None) -> None:
        # One API message can be split over several records that repeat its
        # usage; keep the latest figures per message id instead of summing.
        usage = as_dict(message.get("usage") if message else None)
        if usage is None:
            return
        tokens_in = (
            get_int(usage, "input_tokens")
            + get_int(usage, "cache_creation_input_tokens")
            + get_int(usage, "cache_read_input_tokens")
        )
        tokens_out = get_int(usage, "output_tokens")
        key = get_str(message, "id") or f"anon-{len(self._message_usage)}"
        old_in, old_out = self._message_usage.get(key, (0, 0))
        self._message_usage[key] = (tokens_in, tokens_out)
        self.input_tokens += tokens_in - old_in
        self.output_tokens += tokens_out - old_out

    def _open_tool(self, block: Record | None, at: datetime | None) -> None:
        tool_id = get_str(block, "id")
        name = get_str(block, "name") or "tool"
        if not tool_id or self.step(tool_id) is not None:
            return
        tool_input = as_dict(block.get("input") if block else None)
        if name == "Skill":
            skill = get_str(tool_input, "skill")
            if skill:
                self.skills_used.add(skill)
        detail = summarize_input(name, tool_input)
        if name in AGENT_TOOLS:
            agent_type = get_str(tool_input, "subagent_type")
            detail = f"{detail} ({agent_type})" if agent_type else detail
        step = Step(
            id=tool_id,
            kind="agent" if name in AGENT_TOOLS else "tool",
            name=name,
            detail=detail,
            started_at=at,
        )
        self.steps.append(step)
        self._open[tool_id] = step
        turn = self._turn(at)
        turn.tool_counts[name] = turn.tool_counts.get(name, 0) + 1
        if step.kind == "agent":
            turn.agent_step_ids.append(tool_id)

    def _apply_user(self, record: Record, at: datetime | None) -> None:
        message = as_dict(record.get("message"))
        content = message.get("content") if message else None
        text = text_of(content)
        for body in _NOTIFICATION.findall(text):
            self._apply_notification(body, at)
        blocks = [as_dict(item) for item in as_list(content)]
        results = [b for b in blocks if get_str(b, "type") == "tool_result"]
        if results:
            result = as_dict(record.get("toolUseResult"))
            for block in results:
                self._close_tool(block, result, at)
            return
        if record.get("isMeta") is True or "<task-notification>" in text:
            return
        origin = get_str(as_dict(record.get("origin")), "kind")
        if origin not in (None, "human"):
            return
        # A prompt with a pasted image arrives as a list of blocks.
        if text.strip() or blocks:
            self._add_prompt(record, text.strip() or "(image)", at)

    def _apply_attachment(self, record: Record, at: datetime | None) -> None:
        # A message typed while the agent is busy is delivered as an attachment.
        attachment = as_dict(record.get("attachment"))
        if get_str(attachment, "type") != "queued_command":
            return
        origin = get_str(
            as_dict(attachment.get("origin") if attachment else None), "kind"
        )
        prompt = get_str(attachment, "prompt")
        if prompt and origin in (None, "human"):
            self._add_prompt(record, prompt, at)

    def _add_prompt(self, record: Record, content: str, at: datetime | None) -> None:
        if self.kind == "subagent" and not any(s.kind == "prompt" for s in self.steps):
            self.label = parse_skill_label(content)
        step_id = get_str(record, "uuid") or f"prompt-{len(self.steps)}"
        self.turns.append(Turn(step_id, _snippet(content, 600), at, at))
        self.steps.append(
            Step(
                id=step_id,
                kind="prompt",
                name="Prompt",
                detail=_snippet(content),
                started_at=at,
                ended_at=at,
                status="done",
            )
        )

    def _close_tool(
        self, block: Record | None, result: Record | None, at: datetime | None
    ) -> None:
        tool_id = get_str(block, "tool_use_id")
        step = self._open.get(tool_id) if tool_id else None
        if step is None:
            return
        if step.kind == "agent":
            step.child_agent_id = get_str(result, "agentId") or step.child_agent_id
            if get_str(result, "status") == "async_launched":
                # Background agents report back later through a notification.
                return
        del self._open[step.id]
        step.ended_at = at
        if block and block.get("is_error") is True:
            step.status = "failed"
            step.error = _snippet(text_of(block.get("content")) or "error", 600)
            self.problems.append(Problem(at, "tool_error", step.error, step.id))
            self._turn(at).errors += 1
        else:
            step.status = "done"

    def _apply_notification(self, body: str, at: datetime | None) -> None:
        tool_id = _tag(body, "tool-use-id")
        status = _tag(body, "status") or "completed"
        step = self._open.pop(tool_id, None) if tool_id else None
        if step is None:
            return
        step.ended_at = at
        if status == "completed":
            step.status = "done"
            return
        step.status = "failed"
        summary = _tag(body, "summary") or f"{step.detail} ended with {status}"
        step.error = _snippet(summary, 600)
        self.problems.append(Problem(at, "agent_failed", step.error, step.id))

    def _apply_system(self, record: Record, at: datetime | None) -> None:
        for item in as_list(record.get("hookErrors")):
            if isinstance(item, str) and item.strip():
                self.problems.append(Problem(at, "hook", _snippet(item, 600)))
        if get_str(record, "level") == "error":
            text = get_str(record, "content") or get_str(record, "subtype") or "error"
            self.problems.append(Problem(at, "api_error", _snippet(text, 600)))

    def _apply_pr_link(self, record: Record) -> None:
        url = get_str(record, "prUrl")
        number = get_int(record, "prNumber")
        if not url or any(pr.url == url for pr in self.pull_requests):
            return
        repository = get_str(record, "prRepository") or ""
        self.pull_requests.append(PullRequest(number, url, repository))
