from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, Thread
from typing import Protocol, cast
from uuid import uuid4

from beehaiive.agent_parts.values import resolve_executable

__all__ = [
    "ADVISOR_STEP",
    "AUTONOMOUS_STEPS",
    "AutonomousLifecycleRunner",
    "AutonomousLifecycleService",
    "AutonomousRunResult",
    "CodexSkillExecutor",
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

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "status": self.status,
            "summary": self.summary,
            "handover": dict(self.handover),
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


def _pbi_candidates(repositories: Iterable[object]) -> list[dict[str, object]]:
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
            if planning_status not in {"todo", "backlog"}:
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
    planning_status = _text(context.get("planning_status")).casefold()
    stage = _text(context.get("stage"), "backlog").casefold()
    return 0 if planning_status == "backlog" and stage == "backlog" else 1


def _handover(value: object) -> dict[str, object]:
    source = dict(_mapping(value))
    payload = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 16_000:
        return {"summary": "Handover exceeded the bound and was not retained."}
    return source


class AutonomousLifecycleRunner:
    def __init__(
        self,
        executor: SkillExecutor,
        advisor: SkillExecutor | None = None,
        on_handoff: Callable[[SkillHandoff], None] | None = None,
    ) -> None:
        self.executor = executor
        self.advisor = advisor
        self.on_handoff = on_handoff

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
            try:
                raw_result = self.executor.execute(step, context, handover)
            except Exception as error:
                raw_result = {"status": "blocked", "summary": str(error)}
            result = _mapping(raw_result)
            status = _text(result.get("status"), "succeeded")
            summary = _text(result.get("summary"), f"{step.name} completed")
            handover = {**handover, **_handover(result.get("handover"))}
            handoff = SkillHandoff(step.name, status, summary[:4_000], handover)
            handoffs.append(handoff)
            if self.on_handoff is not None:
                self.on_handoff(handoff)
            if status != "succeeded":
                if self.advisor is not None:
                    advisor_context = {
                        **context,
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
                            _text(advisor_mapping.get("status"), "succeeded"),
                            _text(
                                advisor_mapping.get("summary"),
                                "Advisor handoff recorded",
                            )[:4_000],
                            _handover(advisor_mapping.get("handover")),
                        )
                        if self.on_handoff is not None:
                            self.on_handoff(advisor_handoff)
                    except Exception as advisor_error:
                        advisor_handoff = SkillHandoff(
                            ADVISOR_STEP.name,
                            "failed",
                            str(advisor_error)[:4_000],
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
        timeout_seconds: float = 900.0,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.executable = resolve_executable(executable)
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not Path(step.skill_path).is_file():
            raise FileNotFoundError(f"Skill file not found: {step.skill_path}")
        prompt = (
            "You are one autonomous BeeHAIve lifecycle context. Read and follow "
            f"this skill file exactly: {step.skill_path}\n"
            f"Stage purpose: {step.purpose}\n"
            "Use the supplied PBI context and handover. Keep all linked subtasks "
            "on the same branch. Submit the pull request published, not draft. "
            "Do not re-run review after applying selected fixes. Return one JSON "
            "object with status, summary, and handover. Do not include secrets. "
            "This context is unattended. Never ask for input or wait for approval. "
            "If a material blocker remains, return blocked JSON with its exact "
            "reason.\n"
            f"PBI context: {json.dumps(dict(context), sort_keys=True)}\n"
            f"Handover: {json.dumps(dict(handover), sort_keys=True)}"
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
        if step.model:
            command.extend(("--model", step.model))
        command.append(prompt)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; filename={error.filename!r}; "
                f"errno={error.errno}; message={error.strerror or str(error)}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"Codex timed out after {self.timeout_seconds:g}s: "
                f"executable={self.executable!r}; cwd={str(self.repository)!r}; "
                f"stdout_tail={_output_tail(error.stdout).strip() or '<none>'!r}; "
                f"stderr_tail={_output_tail(error.stderr).strip() or '<none>'!r}"
            ) from error
        except OSError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; errno={error.errno}; "
                f"message={error.strerror or str(error)}"
            ) from error
        if result.returncode != 0:
            detail = _output_tail(result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Codex process failed: executable={self.executable!r}; "
                f"cwd={str(self.repository)!r}; exit_code={result.returncode}; "
                f"output_tail={detail or '<none>'!r}"
            )
        value = _last_json_mapping(result.stdout)
        if value is not None:
            return value
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
                "handoffs": [],
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
        candidates = _pbi_candidates(repositories)
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
    def _completed_or_blocked_items(
        actions: Sequence[Mapping[str, object]],
    ) -> set[tuple[str, int]]:
        latest: dict[tuple[str, int], Mapping[str, object]] = {}
        for action in actions:
            if action.get("kind") != "autonomous_start":
                continue
            repository = action.get("repository")
            pbi_number = action.get("pbi_number")
            if isinstance(repository, str) and type(pbi_number) is int:
                latest.setdefault((repository, pbi_number), action)
        excluded: set[tuple[str, int]] = set()
        for key, action in latest.items():
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

        executor = self._executor_for(context)
        advisor = self._advisor_for(context)
        runner = AutonomousLifecycleRunner(executor, advisor, record)
        try:
            result = runner.run(context)
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
        except Exception as error:
            self.orchestrator.store.finish_action(
                action_id,
                "failed",
                None,
                str(error)[:4_000],
            )
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current.update({"status": "failed", "error": str(error)[:4_000]})
        finally:
            with self._lock:
                self._active_projects.discard(str(context["project_id"]))

    def _executor_for(self, context: Mapping[str, object]) -> SkillExecutor:
        if self._executor is not None:
            return self._executor
        mode = os.environ.get("BEEHAIIVE_AUTONOMOUS_MODE", "codex").strip().lower()
        if mode == "codex":
            return CodexSkillExecutor(
                os.environ.get("BEEHAIIVE_AGENT_REPOSITORY", str(Path.cwd())),
                os.environ.get("BEEHAIIVE_CODEX_EXECUTABLE", "codex"),
                float(os.environ.get("BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS", "900")),
            )
        del context
        return PlaceholderSkillExecutor()

    def _advisor_for(self, context: Mapping[str, object]) -> SkillExecutor | None:
        if self._advisor is not None:
            return self._advisor
        if (
            os.environ.get("BEEHAIIVE_AUTONOMOUS_MODE", "codex").strip().lower()
            == "codex"
        ):
            return self._executor_for(context)
        return PlaceholderSkillExecutor()
