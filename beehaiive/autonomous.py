from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, Thread
from typing import Protocol, cast
from uuid import uuid4

from beehaiive.agent_parts.codex_process_mixin import CodexProcessMixin
from beehaiive.agent_parts.values import resolve_executable
from beehaiive.agent_parts.worker_text import (
    format_worker_exception,
    redact_worker_text,
    safe_worker_environment,
    worker_secret_values,
)

__all__ = [
    "ADVISOR_STEP",
    "AUTONOMOUS_STEPS",
    "AutonomousLifecycleRunner",
    "AutonomousLifecycleService",
    "AutonomousRunResult",
    "CodexSkillExecutor",
    "DEFAULT_AUTONOMOUS_MODEL",
    "DEFAULT_AUTONOMOUS_TIMEOUT_SECONDS",
    "PlaceholderSkillExecutor",
    "SkillHandoff",
    "SkillStep",
    "select_work_item",
]


def _skill_root() -> Path:
    configured = os.environ.get("BEEHAIIVE_DOD_GUARD_SKILLS", "").strip()
    if configured:
        return Path(configured)
    return (
        Path.home()
        / ".codex"
        / "plugins"
        / "cache"
        / "dod-guard-monorepo"
        / "dod-guard"
        / "5.4.5"
        / "skills"
    )


def _skill_path(name: str) -> str:
    return str(_skill_root() / name / "SKILL.md")


def _output_tail(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")[-2_000:]
    return str(value or "")[-2_000:]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _last_json_mapping(output: str) -> Mapping[str, object] | None:
    decoder = json.JSONDecoder()
    best: Mapping[str, object] | None = None
    best_end = -1
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and index + end > best_end:
            best = cast(Mapping[str, object], value)
            best_end = index + end
    return best


@dataclass(frozen=True, slots=True)
class SkillStep:
    name: str
    skill_path: str
    purpose: str
    model: str | None = None


AUTONOMOUS_STEPS: tuple[SkillStep, ...] = (
    SkillStep(
        "refine-backlog-item",
        _skill_path("refine-backlog-item"),
        "Select or refine one backlog PBI and move a coherent item to Todo.",
    ),
    SkillStep(
        "next-ticket",
        _skill_path("next-ticket"),
        "Implement the Todo PBI and its linked subtasks on one branch.",
    ),
    SkillStep(
        "submit-draft-pr",
        _skill_path("submit-draft-pr"),
        "Publish the pull request with fresh acceptance and verification evidence.",
    ),
    SkillStep(
        "review-pr-branch",
        str(Path.home() / ".agents" / "skills" / "review-pr-branch" / "SKILL.md"),
        "Review the pushed branch against the PBI and report concrete findings.",
    ),
    SkillStep(
        "fix-pr-review",
        _skill_path("fix-pr-review"),
        "Fix every selected finding once, then validate without re-reviewing.",
    ),
    SkillStep(
        "complete-pr",
        _skill_path("complete-pr"),
        "Complete the accepted pull request and reconcile the linked PBI.",
    ),
)

ADVISOR_STEP = SkillStep(
    "codex-advisor",
    _skill_path("codex-advisor"),
    "Give one bounded second opinion for a blocker without mutating the repository.",
    model="gpt-5.6-luna",
)

DEFAULT_AUTONOMOUS_MODEL = "gpt-5.6-luna"
DEFAULT_AUTONOMOUS_TIMEOUT_SECONDS: float | None = None


def _autonomous_timeout() -> float | None:
    raw = os.environ.get("BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_AUTONOMOUS_TIMEOUT_SECONDS
    value = float(raw)
    return value if value > 0 else None


class SkillExecutor(Protocol):
    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class AutonomousStore(Protocol):
    def project_state(self, project_id: str) -> dict[str, object]: ...

    def begin_action(
        self,
        project_id: str,
        kind: str,
        request: dict[str, object],
        repository: str | None = None,
        pbi_number: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]: ...

    def finish_action(
        self,
        action_id: str,
        status: str,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> dict[str, object]: ...

    def actions_for_project(
        self, project_id: str, limit: int = 100
    ) -> list[dict[str, object]]: ...


class AutonomousOrchestrator(Protocol):
    @property
    def store(self) -> AutonomousStore: ...


@dataclass(frozen=True, slots=True)
class SkillHandoff:
    step: str
    status: str
    summary: str
    handover: Mapping[str, object]
    session_output: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "status": self.status,
            "summary": self.summary,
            "handover": dict(self.handover),
            "session_output": self.session_output,
        }


@dataclass(frozen=True, slots=True)
class AutonomousRunResult:
    run_id: str
    status: str
    repository: str
    pbi_number: int
    handoffs: tuple[SkillHandoff, ...]
    error: str | None = None
    advisor: SkillHandoff | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "repository": self.repository,
            "pbi_number": self.pbi_number,
            "handoffs": [handoff.as_dict() for handoff in self.handoffs],
            "error": self.error,
            "advisor": self.advisor.as_dict() if self.advisor else None,
        }


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _text(value: object, fallback: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _pbi_candidates(
    repositories: Iterable[object], *, include_in_progress: bool = False
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for raw_repository in repositories:
        repository = _mapping(raw_repository)
        repository_name = _text(repository.get("name"))
        if not repository_name or repository.get("active") is False:
            continue
        raw_pbis = repository.get("pbis", ())
        if not isinstance(raw_pbis, Sequence) or isinstance(
            raw_pbis, (str, bytes, bytearray)
        ):
            continue
        for raw_pbi in cast(Sequence[object], raw_pbis):
            pbi = _mapping(raw_pbi)
            if pbi.get("archived") is True or pbi.get("active") is False:
                continue
            if _text(pbi.get("status")).casefold() in {"active", "awaiting_operator"}:
                continue
            planning_status = _text(pbi.get("planning_status"), "backlog").casefold()
            if planning_status not in {"todo", "backlog"} and not (
                include_in_progress and planning_status == "in progress"
            ):
                continue
            candidates.append(
                {
                    "repository": repository_name,
                    "pbi_number": pbi.get("number"),
                    "title": _text(pbi.get("title"), "Untitled PBI"),
                    "planning_status": planning_status,
                    "stage": _text(pbi.get("stage"), "backlog"),
                    "subtasks": pbi.get("subtasks", []),
                }
            )
    return candidates


def select_work_item(repositories: Iterable[object]) -> dict[str, object] | None:
    candidates = _pbi_candidates(repositories)
    candidates.sort(key=_candidate_sort_key)
    return candidates[0] if candidates else None


def _candidate_sort_key(item: Mapping[str, object]) -> tuple[int, int]:
    raw_number = item.get("pbi_number")
    number = raw_number if type(raw_number) is int else 2_147_483_647
    return (0 if item.get("planning_status") == "todo" else 1, number)


def _start_index(context: Mapping[str, object]) -> int:
    requested_step = _text(context.get("resume_step")).casefold()
    if requested_step:
        for index, step in enumerate(AUTONOMOUS_STEPS):
            if step.name == requested_step:
                return index
    planning_status = _text(context.get("planning_status")).casefold()
    return 0 if planning_status == "backlog" else 1


def _handover(value: object) -> dict[str, object]:
    source = dict(_mapping(value))
    payload = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 16_000:
        return {"summary": "Handover exceeded the bound and was not retained."}
    return source


def _skill_status(value: object) -> str:
    status = _text(value, "succeeded").casefold()
    return (
        "succeeded"
        if status
        in {
            "complete",
            "completed",
            "done",
            "fixed",
            "merged",
            "passed",
            "published",
            "reviewed",
            "success",
            "succeeded",
        }
        else status
    )


def _context_for_step(
    context: Mapping[str, object], handover: Mapping[str, object]
) -> dict[str, object]:
    updated = dict(context)
    project_status = _text(handover.get("project_status"))
    if project_status:
        updated["planning_status"] = project_status
    next_stage = _text(handover.get("next_stage")).casefold()
    stage = {
        "implementation": "implement",
        "implement": "implement",
        "review": "review",
        "pull_request": "pull_request",
        "merge": "merge",
        "ship": "merge",
    }.get(next_stage)
    if stage:
        updated["stage"] = stage
    return updated


def _session_output(stdout: str, stderr: str) -> str:
    sections = []
    if stdout.strip():
        sections.append(f"[stdout]\n{stdout.strip()}")
    if stderr.strip():
        sections.append(f"[stderr]\n{stderr.strip()}")
    output = redact_worker_text(
        "\n\n".join(sections), worker_secret_values(), max_length=None
    )
    if len(output) <= 16_000:
        return output
    half = 8_000
    return f"{output[:half]}\n...[session output truncated]...\n{output[-half:]}"


class AutonomousLifecycleRunner:
    def __init__(
        self,
        executor: SkillExecutor,
        advisor: SkillExecutor | None = None,
        on_handoff: Callable[[SkillHandoff], None] | None = None,
        on_step: Callable[[str], None] | None = None,
    ) -> None:
        self.executor = executor
        self.advisor = advisor
        self.on_handoff = on_handoff
        self.on_step = on_step

    def run(self, context: Mapping[str, object]) -> AutonomousRunResult:
        repository = _text(context.get("repository"), "unknown repository")
        pbi_number = context.get("pbi_number")
        if type(pbi_number) is not int or pbi_number <= 0:
            raise ValueError("An autonomous run requires a positive PBI number")
        run_id = _text(context.get("run_id"), str(uuid4()))
        handover: dict[str, object] = {
            "run_id": run_id,
            "repository": repository,
            "pbi_number": pbi_number,
            "single_branch": True,
            "subtasks_same_branch": True,
        }
        handoffs: list[SkillHandoff] = []
        advisor_handoff: SkillHandoff | None = None
        steps = AUTONOMOUS_STEPS[_start_index(context) :]
        for step in steps:
            if self.on_step is not None:
                self.on_step(step.name)
            step_context = _context_for_step(context, handover)
            try:
                raw_result = self.executor.execute(step, step_context, handover)
            except Exception as error:
                raw_result = {
                    "status": "blocked",
                    "summary": format_worker_exception(error),
                    "session_output": getattr(error, "session_output", ""),
                }
            result = _mapping(raw_result)
            status = _skill_status(result.get("status"))
            summary = _text(result.get("summary"), f"{step.name} completed")
            handover = {**handover, **_handover(result.get("handover"))}
            handoff = SkillHandoff(
                step.name,
                status,
                summary[:4_000],
                handover,
                _text(result.get("session_output")) or None,
            )
            handoffs.append(handoff)
            if self.on_handoff is not None:
                self.on_handoff(handoff)
            if status != "succeeded":
                if self.advisor is not None:
                    advisor_context = {
                        **step_context,
                        "failed_step": step.name,
                        "failure": summary[:4_000],
                        "completed_steps": [item.step for item in handoffs],
                    }
                    try:
                        advisor_result = self.advisor.execute(
                            ADVISOR_STEP, advisor_context, handover
                        )
                        advisor_mapping = _mapping(advisor_result)
                        advisor_handoff = SkillHandoff(
                            ADVISOR_STEP.name,
                            _skill_status(advisor_mapping.get("status")),
                            _text(
                                advisor_mapping.get("summary"),
                                "Advisor handoff recorded",
                            )[:4_000],
                            _handover(advisor_mapping.get("handover")),
                            _text(advisor_mapping.get("session_output")) or None,
                        )
                        if self.on_handoff is not None:
                            self.on_handoff(advisor_handoff)
                    except Exception as advisor_error:
                        advisor_handoff = SkillHandoff(
                            ADVISOR_STEP.name,
                            "failed",
                            format_worker_exception(advisor_error)[:4_000],
                            handover,
                        )
                        if self.on_handoff is not None:
                            self.on_handoff(advisor_handoff)
                return AutonomousRunResult(
                    run_id,
                    "blocked",
                    repository,
                    pbi_number,
                    tuple(handoffs),
                    error=summary[:4_000],
                    advisor=advisor_handoff,
                )
        return AutonomousRunResult(
            run_id, "completed", repository, pbi_number, tuple(handoffs)
        )


class PlaceholderSkillExecutor:
    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]:
        try:
            remaining = AUTONOMOUS_STEPS[AUTONOMOUS_STEPS.index(step) + 1 :]
        except ValueError:
            remaining = ()
        next_step = remaining[0].name if remaining else "complete"
        return {
            "status": "succeeded",
            "summary": f"Placeholder context completed {step.name}.",
            "handover": {
                **dict(handover),
                "completed_skill": step.name,
                "next_skill": next_step,
                "context_project": context.get("project_id"),
            },
        }


class CodexSkillExecutor:
    def __init__(
        self,
        repository: str | Path,
        executable: str = "codex",
        timeout_seconds: float | None = None,
        on_output: Callable[[str], None] | None = None,
        on_process: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.executable = resolve_executable(executable)
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        )
        self.on_output = on_output
        self.on_process = on_process

    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not Path(step.skill_path).is_file():
            raise FileNotFoundError(f"Skill file not found: {step.skill_path}")
        workspace_instruction = ""
        if context.get("workspace_path") and context.get("workspace_branch"):
            workspace_instruction = (
                f"The server assigned checkout {context['workspace_path']!r} "
                f"on branch {context['workspace_branch']!r}. Use that checkout "
                "and branch for every lifecycle step. Do not create another "
                "checkout or branch, and do not modify the server checkout.\n"
            )
        prompt = (
            "You are one autonomous BeeHAIve lifecycle context. Read and follow "
            f"this skill file exactly: {step.skill_path}\n"
            f"Stage purpose: {step.purpose}\n"
            "Use the supplied PBI context and handover. Keep all linked subtasks "
            "on the same branch. Submit the pull request published, not draft. "
            "Do not re-run review after applying selected fixes. Return one JSON "
            "object with status, summary, and handover. Do not include secrets. "
            f"The scheduler already assigned PBI #{context.get('pbi_number')}. "
            "Do not ask the operator to choose another PBI. This context is "
            "unattended. Never ask for input or wait for approval. The JSON is "
            "an internal handover after doing the stage work, not a substitute "
            "for doing it. For non-destructive unresolved choices, choose the "
            "smallest conservative reversible default, record that decision in "
            "the issue or evidence, and continue. Stop only for missing authority, "
            "credentials, or a destructive policy choice that cannot be made safely. "
            "If a material blocker remains, return blocked JSON with its exact "
            "reason.\n"
            f"{workspace_instruction}"
            f"PBI context: {json.dumps(dict(context), sort_keys=True)}\n"
            f"Handover: {json.dumps(dict(handover), sort_keys=True)}"
        )
        if step.name == "next-ticket":
            prompt += (
                "\nThis is one stage inside a larger autonomous lifecycle. The outer "
                "runner invokes the separate review-pr-branch stage after this "
                "one. Do not invoke collab, spawn a nested reviewer, or wait for "
                "that review here. Complete implementation, verification, commit, "
                "and push, then return the handover."
            )
        command = [
            self.executable,
            "--approve-for-me",
            "exec",
            "--color",
            "never",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(self.repository),
        ]
        command.extend(("--model", DEFAULT_AUTONOMOUS_MODEL))
        command.append(prompt)
        started_at = _now_iso()
        process = None
        try:
            try:
                process = CodexProcessMixin._start_process(
                    command,
                    safe_worker_environment(),
                )
            except Exception as error:
                if self.on_process is not None:
                    self.on_process(
                        {
                            "state": "launch_failed",
                            "finished_at": _now_iso(),
                            "executable": self.executable,
                            "cwd": str(self.repository),
                            "model": DEFAULT_AUTONOMOUS_MODEL,
                            "error": format_worker_exception(error),
                        }
                    )
                raise
            if self.on_process is not None:
                self.on_process(
                    {
                        "state": "running",
                        "pid": process.pid,
                        "started_at": started_at,
                        "executable": self.executable,
                        "cwd": str(self.repository),
                        "model": DEFAULT_AUTONOMOUS_MODEL,
                        "timeout_seconds": self.timeout_seconds,
                    }
                )
            timed_out = False
            process_state = "running"
            try:
                stdout, stderr, timed_out = CodexProcessMixin._communicate_bounded(
                    process,
                    self.timeout_seconds,
                    self.on_output,
                    stderr_line_handler=self.on_output,
                    terminate_descendants=True,
                )
                process_state = "timed_out" if timed_out else "exited"
            except subprocess.TimeoutExpired:
                CodexProcessMixin._terminate_process(process)
                stdout, stderr = "", ""
                timed_out = True
                process_state = "timed_out"
            except Exception:
                process_state = "error"
                raise
            finally:
                if self.on_process is not None:
                    self.on_process(
                        {
                            "state": process_state,
                            "pid": process.pid,
                            "finished_at": _now_iso(),
                            "returncode": process.returncode,
                        }
                    )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; details:\n"
                f"{format_worker_exception(error)}"
            ) from error
        except OSError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; details:\n"
                f"{format_worker_exception(error)}"
            ) from error
        session_output = _session_output(stdout, stderr)
        if timed_out:
            error = RuntimeError(
                f"Codex timed out after {self.timeout_seconds:g}s: "
                f"started_at={started_at!r}; executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; "
                f"session_tail={_output_tail(session_output).strip() or '<none>'!r}"
            )
            error.session_output = session_output  # type: ignore[attr-defined]
            raise error
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        if result.returncode != 0:
            detail = _output_tail(result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Codex process failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; exit_code={result.returncode}; "
                f"output_tail={detail or '<none>'!r}"
            )
        value = _last_json_mapping(result.stdout)
        if value is not None:
            return {
                **value,
                "session_output": session_output,
            }
        raise RuntimeError(
            f"Codex skill context returned no JSON handover: "
            f"executable={self.executable!r}; cwd={str(self.repository)!r}; "
            f"output_tail={_output_tail(result.stdout).strip() or '<none>'!r}"
        )


class AutonomousLifecycleService:
    """Run one PBI lifecycle in separate background skill contexts."""

    def __init__(
        self,
        orchestrator: AutonomousOrchestrator,
        executor: SkillExecutor | None = None,
        advisor: SkillExecutor | None = None,
        max_concurrency: int = 1,
        workflow_service: object | None = None,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ValueError("Autonomous worker capacity must be a positive integer")
        self.orchestrator = orchestrator
        self._runs: dict[str, dict[str, object]] = {}
        self._active_projects: set[str] = set()
        self._max_concurrency = max_concurrency
        configured_repository = os.environ.get(
            "BEEHAIIVE_AGENT_REPOSITORY_NAME", ""
        ).strip()
        self._configured_repository = configured_repository or None
        self._lock = RLock()
        self._executor = executor
        self._advisor = advisor
        self._workflow_service = workflow_service
        self._workspaces: dict[str, object] = {}

    def start(
        self,
        project_id: str,
        repository: str | None = None,
        pbi_number: int | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if project_id in self._active_projects:
                raise ValueError("An autonomous lifecycle is already running")
            if len(self._active_projects) >= self._max_concurrency:
                raise ValueError("Autonomous worker capacity is full")
            selected_repository = repository or self._configured_repository
            if (
                repository is not None
                and self._configured_repository is not None
                and repository != self._configured_repository
            ):
                raise ValueError("Requested PBI is outside the configured checkout")
            state = self.orchestrator.store.project_state(project_id)
            excluded: set[tuple[str, int]] = (
                set()
                if pbi_number is not None
                else self._completed_or_blocked_items(
                    self.orchestrator.store.actions_for_project(project_id)
                )
            )
            selected = self._select(state, selected_repository, pbi_number, excluded)
            if selected is None:
                raise ValueError("No eligible Todo or Backlog PBI is available")
            selected_number = selected.get("pbi_number")
            if type(selected_number) is not int or selected_number <= 0:
                raise ValueError("Selected PBI number is invalid")
            run_id = str(uuid4())
            context = {"project_id": project_id, **selected, "run_id": run_id}
            if pbi_number is not None:
                context.update(
                    self._resume_context(
                        self.orchestrator.store.actions_for_project(project_id),
                        str(selected["repository"]),
                        selected_number,
                    )
                )
            action = self.orchestrator.store.begin_action(
                project_id,
                "autonomous_start",
                {"repository": selected["repository"], "pbi_number": selected_number},
                str(selected["repository"]),
                selected_number,
                run_id,
            )
            self._active_projects.add(project_id)
            self._runs[run_id] = {
                "run_id": run_id,
                "project_id": project_id,
                "repository": selected["repository"],
                "pbi_number": selected_number,
                "status": "running",
                "current_step": None,
                "started_at": _now_iso(),
                "step_started_at": None,
                "last_output_at": None,
                "last_output": None,
                "process": {"state": "starting"},
                "handoffs": [],
                "session_events": [],
                "action_id": action["id"],
            }
        Thread(
            target=self._run,
            args=(run_id, context, str(action["id"])),
            name=f"beehaiive-autonomous-{run_id[:8]}",
            daemon=True,
        ).start()
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, object]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ValueError("Unknown autonomous run")
            return dict(run)

    def status_for_work_item(
        self, project_id: str, repository: str, pbi_number: int
    ) -> dict[str, object] | None:
        with self._lock:
            for run in self._runs.values():
                if (
                    run.get("project_id") == project_id
                    and run.get("repository") == repository
                    and run.get("pbi_number") == pbi_number
                ):
                    return dict(run)
        return None

    def has_capacity(self) -> bool:
        with self._lock:
            return len(self._active_projects) < self._max_concurrency

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_projects)

    def set_max_concurrency(self, maximum: int) -> None:
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("Autonomous worker capacity must be a positive integer")
        with self._lock:
            self._max_concurrency = maximum

    def recover_pending(self, project_ids: Iterable[str]) -> int:
        recovered = 0
        for project_id in project_ids:
            for action in self.orchestrator.store.actions_for_project(project_id):
                if (
                    action.get("kind") != "autonomous_start"
                    or action.get("status") != "pending"
                ):
                    continue
                detail = (
                    "Autonomous run did not survive the server restart; "
                    "no live worker process is attached."
                )
                self.orchestrator.store.finish_action(
                    str(action["id"]),
                    "failed",
                    {"status": "blocked", "error": detail},
                    detail,
                )
                recovered += 1
        return recovered

    def _select(
        self,
        state: Mapping[str, object],
        repository: str | None,
        pbi_number: int | None,
        excluded: set[tuple[str, int]],
    ) -> dict[str, object] | None:
        raw_repositories = state.get("repositories", ())
        repositories = (
            cast(Sequence[object], raw_repositories)
            if isinstance(raw_repositories, Sequence)
            and not isinstance(raw_repositories, (str, bytes, bytearray))
            else ()
        )
        candidates = _pbi_candidates(
            repositories, include_in_progress=pbi_number is not None
        )
        if repository is not None:
            candidates = [
                item for item in candidates if item["repository"] == repository
            ]
        if pbi_number is not None:
            candidates = [
                item for item in candidates if item["pbi_number"] == pbi_number
            ]
        candidates = [
            item
            for item in candidates
            if not (
                isinstance(item.get("repository"), str)
                and type(item.get("pbi_number")) is int
                and (item["repository"], item["pbi_number"]) in excluded
            )
        ]
        candidates.sort(key=lambda item: _candidate_sort_key(item))
        return candidates[0] if candidates else None

    @staticmethod
    def _resume_context(
        actions: Sequence[Mapping[str, object]], repository: str, pbi_number: int
    ) -> dict[str, object]:
        step_names = [step.name for step in AUTONOMOUS_STEPS]
        for action in actions:
            if (
                action.get("repository") != repository
                or action.get("pbi_number") != pbi_number
            ):
                continue
            kind = _text(action.get("kind"))
            if not kind.startswith("skill:") or kind == "skill:codex-advisor":
                continue
            step = kind[6:]
            if step not in step_names:
                continue
            result = _mapping(action.get("result"))
            handover = _mapping(result.get("handover"))
            published = step == "submit-draft-pr" and (
                handover.get("draft") is False
                or bool(handover.get("pull_request"))
            )
            if action.get("status") != "succeeded" and not (
                _skill_status(result.get("status")) == "succeeded" or published
            ):
                if not handover.get("branch") and not handover.get(
                    "workspace_branch"
                ):
                    continue
                return {}
            next_index = step_names.index(step) + 1
            if next_index >= len(step_names):
                return {}
            resume: dict[str, object] = {
                "resume_step": step_names[next_index],
                "resume_existing_workspace": True,
            }
            for key in ("branch", "pr", "pull_request", "head", "head_commit"):
                value = handover.get(key)
                if isinstance(value, (str, int)):
                    resume[key] = value
                    if key == "branch":
                        resume["workspace_branch"] = value
            return resume
        return {}

    @staticmethod
    def _completed_or_blocked_items(
        actions: Sequence[Mapping[str, object]],
    ) -> set[tuple[str, int]]:
        latest: dict[tuple[str, int], Mapping[str, object]] = {}
        for action in actions:
            if action.get("kind") not in {"autonomous_start", "requeue"}:
                continue
            repository = action.get("repository")
            pbi_number = action.get("pbi_number")
            if isinstance(repository, str) and type(pbi_number) is int:
                latest.setdefault((repository, pbi_number), action)
        excluded: set[tuple[str, int]] = set()
        for key, action in latest.items():
            if action.get("kind") == "requeue" and action.get("status") == "succeeded":
                continue
            result = _mapping(action.get("result"))
            if (
                action.get("status") in {"pending", "failed"}
                or result.get("status") == "completed"
            ):
                excluded.add(key)
        return excluded

    def _run(
        self,
        run_id: str,
        context: Mapping[str, object],
        action_id: str,
    ) -> None:
        def record_step(step: str) -> None:
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["current_step"] = step
                    current["step_started_at"] = _now_iso()
                    current["last_output_at"] = None
                    current["last_output"] = None
                    current["process"] = {"state": "starting"}

        def record_process(process: Mapping[str, object]) -> None:
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["process"] = {
                        **_mapping(current.get("process")),
                        **dict(process),
                    }

        def record_output(line: str) -> None:
            if not line.strip():
                return
            event = {
                "kind": "output",
                "source_type": "codex",
                "text": redact_worker_text(line, worker_secret_values()),
                "created_at": datetime.now(UTC).isoformat(),
            }
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["last_output_at"] = event["created_at"]
                    current["last_output"] = event["text"]
                    current["session_events"] = [
                        *cast(list[dict[str, object]], current["session_events"]),
                        event,
                    ][-100:]

        def record(handoff: SkillHandoff) -> None:
            pbi_number = context.get("pbi_number")
            if type(pbi_number) is not int or pbi_number <= 0:
                raise ValueError("Autonomous handoff has an invalid PBI number")
            step_action = self.orchestrator.store.begin_action(
                str(context["project_id"]),
                f"skill:{handoff.step}",
                handoff.as_dict(),
                str(context["repository"]),
                pbi_number,
                run_id,
            )
            self.orchestrator.store.finish_action(
                str(step_action["id"]),
                "succeeded" if handoff.status == "succeeded" else "failed",
                handoff.as_dict(),
                None if handoff.status == "succeeded" else handoff.summary,
            )
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["current_step"] = handoff.step
                    current["handoffs"] = [
                        *cast(list[dict[str, object]], current["handoffs"]),
                        handoff.as_dict(),
                    ]

        workspace_context = dict(context)
        workspace_created = False
        try:
            workspace_context = self._prepare_workspace(run_id, workspace_context)
            workspace_created = "workspace_path" in workspace_context
            executor = self._executor_for(
                workspace_context, record_output, record_process
            )
            advisor = self._advisor_for(
                workspace_context, record_output, record_process
            )
            runner = AutonomousLifecycleRunner(
                executor, advisor, record, on_step=record_step
            )
            result = runner.run(workspace_context)
            if workspace_created:
                self._release_workspace(run_id)
                workspace_created = False
            self.orchestrator.store.finish_action(
                action_id,
                "succeeded" if result.status == "completed" else "failed",
                result.as_dict(),
                result.error,
            )
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    recorded_handoffs = cast(
                        list[dict[str, object]], current["handoffs"]
                    )
                    current.update(result.as_dict())
                    current["handoffs"] = recorded_handoffs
                    current["current_step"] = None
                    current["finished_at"] = _now_iso()
        except Exception as error:
            detail = format_worker_exception(error)
            self.orchestrator.store.finish_action(
                action_id,
                "failed",
                None,
                redact_worker_text(detail)[:4_000],
            )
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current.update(
                        {
                            "status": "failed",
                            "error": redact_worker_text(detail)[:4_000],
                            "finished_at": _now_iso(),
                        }
                    )
        finally:
            if workspace_created:
                self._release_workspace(run_id)
            with self._lock:
                self._active_projects.discard(str(context["project_id"]))

    def _prepare_workspace(
        self, run_id: str, context: dict[str, object]
    ) -> dict[str, object]:
        mode = os.environ.get("BEEHAIIVE_AUTONOMOUS_MODE", "codex").strip().lower()
        if self._executor is not None or mode != "codex":
            return context
        service = self._workflow_service
        if service is None:
            raise RuntimeError(
                "Autonomous Codex execution requires the server workflow service; "
                "no checkout was touched"
            )
        worktrees = getattr(service, "worktrees", None)
        repository = getattr(worktrees, "repository", None)
        acquire = getattr(service, "acquire_workspace", None)
        if not isinstance(repository, Path) or not callable(acquire):
            raise RuntimeError(
                "Autonomous Codex execution requires a server-managed repository "
                "worktree service; no checkout was touched"
            )
        workspace_root = (
            repository.parent
            / f".{repository.name}.beehaiive"
            / "autonomous-worktrees"
        )
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_id = uuid4().hex
        branch = _text(
            context.get("workspace_branch"),
            f"codex/beehaiive-autonomous-{run_id[:12]}",
        )
        base_ref = os.environ.get("BEEHAIIVE_AUTONOMOUS_BASE_REF", "origin/master")
        run_git = getattr(worktrees, "run_git", None)
        if callable(run_git):
            try:
                if run_git("rev-parse", "--verify", base_ref).returncode != 0:
                    base_ref = "HEAD"
            except Exception:
                base_ref = "HEAD"
        workspace_path = workspace_root / workspace_id
        if context.get("resume_existing_workspace"):
            acquire_existing = getattr(worktrees, "acquire_existing", None)
            if not isinstance(branch, str) or not callable(acquire_existing):
                raise RuntimeError(
                    "Autonomous resume requires an existing branch and worktree "
                    "manager"
                )
            lease = acquire_existing(
                f"dashboard-run:{run_id}", branch, workspace_path
            )
        else:
            lease = acquire(
                f"dashboard-run:{run_id}", branch, workspace_path, base_ref
            )
        with self._lock:
            self._workspaces[run_id] = lease
        return {
            **context,
            "workspace_path": lease.worktree_path,
            "workspace_branch": lease.branch,
        }

    def _release_workspace(self, run_id: str) -> None:
        with self._lock:
            lease = self._workspaces.pop(run_id, None)
        service = self._workflow_service
        if lease is None or service is None:
            return
        lease_id = getattr(lease, "lease_id", None)
        lease_token = getattr(lease, "lease_token", None)
        worktrees = getattr(service, "worktrees", None)
        if not isinstance(lease_id, str) or not callable(
            getattr(service, "release_workspace", None)
        ):
            return
        try:
            clean = bool(worktrees.clean(lease.worktree_path))
        except Exception:
            clean = False
        try:
            if clean:
                service.release_workspace(lease_id)
            elif callable(getattr(service, "retain_workspace", None)):
                service.retain_workspace(lease_id, lease_token)
        except Exception as error:
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["workspace_cleanup_error"] = redact_worker_text(
                        format_worker_exception(error)
                    )[:4_000]

    def _executor_for(
        self,
        context: Mapping[str, object],
        on_output: Callable[[str], None] | None = None,
        on_process: Callable[[Mapping[str, object]], None] | None = None,
    ) -> SkillExecutor:
        if self._executor is not None:
            return self._executor
        mode = os.environ.get("BEEHAIIVE_AUTONOMOUS_MODE", "codex").strip().lower()
        if mode == "codex":
            repository = context.get("workspace_path")
            if not isinstance(repository, str) or not repository.strip():
                raise RuntimeError(
                    "Autonomous Codex execution has no leased worktree; "
                    "no checkout was touched"
                )
            return CodexSkillExecutor(
                repository,
                os.environ.get("BEEHAIIVE_CODEX_EXECUTABLE", "codex"),
                _autonomous_timeout(),
                on_output=on_output,
                on_process=on_process,
            )
        del context
        return PlaceholderSkillExecutor()

    def _advisor_for(
        self,
        context: Mapping[str, object],
        on_output: Callable[[str], None] | None = None,
        on_process: Callable[[Mapping[str, object]], None] | None = None,
    ) -> SkillExecutor | None:
        if self._advisor is not None:
            return self._advisor
        if (
            os.environ.get("BEEHAIIVE_AUTONOMOUS_MODE", "codex").strip().lower()
            == "codex"
        ):
            return self._executor_for(context, on_output, on_process)
        return PlaceholderSkillExecutor()
