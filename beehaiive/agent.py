"""Bounded local worker used by the dashboard demo."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import TYPE_CHECKING, Any, cast

from .models import RunState, RunStatus, Stage
from .routing import (
    AttemptOutcome,
    ModelExecution,
    ModelSpec,
    RoutingDecision,
    RoutingStatus,
)
from .storage import StoreError

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


DEMO_TASK_NAME = "bounded repository inventory"
DEFAULT_DEMO_TASK = (
    "Inspect only the current checkout and report the repository name, current "
    "branch, and count of tracked files. Do not edit files, create files, access "
    "the network, read credentials, or start other agents. Return a concise "
    "plain-text result."
)
MAX_AGENT_OUTPUT_LENGTH = 4_000
_SECRET_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY)", re.IGNORECASE
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def redact_worker_text(text: str, secret_values: tuple[str, ...] = ()) -> str:
    """Remove common credential forms before worker text reaches durable state."""

    redacted = text
    for secret in sorted(
        (value for value in secret_values if value), key=len, reverse=True
    ):
        redacted = redacted.replace(secret, "[redacted]")
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]", redacted
    )[:MAX_AGENT_OUTPUT_LENGTH]


class CodexExecModelExecutor:
    """Run one read-only, ephemeral ``codex exec`` process per routing attempt."""

    def __init__(
        self,
        repository: Path,
        *,
        executable: str = "codex",
        timeout_seconds: float = 120.0,
        model: str | None = None,
        task: str = DEFAULT_DEMO_TASK,
    ) -> None:
        resolved_repository = repository.resolve()
        if not resolved_repository.is_dir():
            raise ValueError(f"Agent repository does not exist: {resolved_repository}")
        if not executable.strip():
            raise ValueError("Agent executable is required")
        if timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        if not task.strip():
            raise ValueError("Agent task is required")
        self.repository = resolved_repository
        self.executable = executable.strip()
        self.timeout_seconds = timeout_seconds
        self.model = model.strip() if model and model.strip() else None
        self.task = task.strip()
        self._lock = Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._secret_values = tuple(
            value
            for name, value in os.environ.items()
            if _SECRET_NAME.search(name) and len(value) >= 4
        )

    @classmethod
    def from_environment(cls) -> CodexExecModelExecutor:
        """Build the clean-device executor from documented environment settings."""

        timeout_value = os.environ.get("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "120")
        try:
            timeout_seconds = float(timeout_value)
        except ValueError as exc:
            raise ValueError(
                "BEEHAIIVE_AGENT_TIMEOUT_SECONDS must be a positive number"
            ) from exc
        return cls(
            Path(
                os.environ.get(
                    "BEEHAIIVE_AGENT_REPOSITORY",
                    str(Path(__file__).resolve().parent.parent),
                )
            ),
            executable=os.environ.get("BEEHAIIVE_CODEX_EXECUTABLE", "codex"),
            timeout_seconds=timeout_seconds,
            model=os.environ.get("BEEHAIIVE_CODEX_MODEL"),
        )

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        """Run the fixed safe task and return only its final agent message."""

        problem_id = decision.problem_id
        prompt = self._prompt(spec, decision)
        with self._lock:
            if problem_id in self._cancelled:
                self._cancelled.discard(problem_id)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
        process = self._start_process(self._command(prompt), self._safe_environment())
        with self._lock:
            self._processes[problem_id] = process
            cancelled = problem_id in self._cancelled
        if cancelled:
            self._terminate_process(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                stdout, stderr = process.communicate()
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context=(
                        f"Agent timed out after {self.timeout_seconds:g} seconds"
                    ),
                )
        finally:
            with self._lock:
                self._processes.pop(problem_id, None)
                was_cancelled = problem_id in self._cancelled
                self._cancelled.discard(problem_id)
        if was_cancelled:
            return ModelExecution(
                AttemptOutcome.FAILURE,
                failure_context="Agent stopped by operator",
            )

        result, input_tokens, output_tokens = self._parse_output(stdout)
        result = redact_worker_text(result, self._secret_values).strip()
        if process.returncode != 0:
            detail = redact_worker_text(stderr or stdout, self._secret_values).strip()
            if not detail:
                detail = f"codex exec exited with status {process.returncode}"
            return ModelExecution(
                AttemptOutcome.FAILURE,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                failure_context=f"Bounded agent failed: {detail}",
            )
        if not result:
            return ModelExecution(
                AttemptOutcome.FAILURE,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                failure_context="Bounded agent returned no result",
            )
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            result=result,
        )

    def cancel(self, problem_id: str) -> None:
        """Terminate the process for one run, including its child process tree."""

        with self._lock:
            self._cancelled.add(problem_id)
            process = self._processes.get(problem_id)
        if process is not None:
            self._terminate_process(process)

    def _prompt(self, spec: ModelSpec, decision: RoutingDecision) -> str:
        return (
            "BeeHAIve dashboard demo.\n"
            f"Task name: {DEMO_TASK_NAME}\n"
            f"Task: {self.task}\n"
            f"Routing tier: {spec.tier.value}. Routing reason: {decision.reason}.\n"
            "The task is read-only. Do not report success unless the inspection "
            "completed."
        )

    def _command(self, prompt: str) -> list[str]:
        command = [
            self.executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            str(self.repository),
        ]
        if self.model is not None:
            command.extend(("--model", self.model))
        command.append(prompt)
        return command

    def _safe_environment(self) -> dict[str, str]:
        return {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("BEEHAIIVE_") and not _SECRET_NAME.search(name)
        }

    @staticmethod
    def _start_process(
        command: list[str], environment: dict[str, str]
    ) -> subprocess.Popen[str]:
        common: dict[str, Any] = {
            "cwd": str(Path(command[command.index("--cd") + 1])),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            return subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                **common,
            )
        return subprocess.Popen(command, start_new_session=True, **common)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
            else:
                process.kill()
            process.wait(timeout=3)

    @staticmethod
    def _parse_output(output: str) -> tuple[str, int, int]:
        final_message = ""
        fallback_lines: list[str] = []
        input_tokens = 0
        output_tokens = 0
        for line in output.splitlines():
            try:
                raw_event: object = json.loads(line)
            except json.JSONDecodeError:
                fallback_lines.append(line)
                continue
            if not isinstance(raw_event, dict):
                continue
            event = cast(dict[str, object], raw_event)
            usage_value = event.get("usage")
            usage = (
                cast(dict[str, object], usage_value)
                if isinstance(usage_value, dict)
                else None
            )
            if isinstance(usage, Mapping):
                input_tokens = _nonnegative_int(usage.get("input_tokens"), input_tokens)
                output_tokens = _nonnegative_int(
                    usage.get("output_tokens"), output_tokens
                )
            item_value = event.get("item")
            item = (
                cast(dict[str, object], item_value)
                if isinstance(item_value, dict)
                else None
            )
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                message = _text_value(item.get("text", item.get("content")))
                if message:
                    final_message = message
            elif event.get("type") == "agent_message":
                message = _text_value(event.get("text", event.get("content")))
                if message:
                    final_message = message
        if not final_message:
            final_message = "\n".join(fallback_lines).strip()
        if input_tokens == 0:
            input_tokens = max(1, len(output) // 4)
        if output_tokens == 0:
            output_tokens = max(1, len(final_message) // 4)
        return final_message, input_tokens, output_tokens


class AgentWorkerManager:
    """Start, stop, and clean up one bounded worker per dashboard run."""

    def __init__(
        self, orchestrator: Orchestrator, executor: CodexExecModelExecutor
    ) -> None:
        self.orchestrator = orchestrator
        self.executor = executor
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}

    def start(self, run: RunState) -> None:
        if run.status is not RunStatus.ACTIVE or run.lease_token is None:
            raise StoreError("An active leased run is required")
        with self._lock:
            if run.run_id in self._threads:
                raise StoreError("An agent worker is already active")
            thread = Thread(
                target=self._run,
                args=(run.run_id, run.lease_token),
                name=f"beehaiive-agent-{run.run_id[:8]}",
                daemon=True,
            )
            self._threads[run.run_id] = thread
        thread.start()

    def cancel(self, run_id: str) -> None:
        self.executor.cancel(run_id)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self._threads.items())
        for run_id, _thread in items:
            self.cancel(run_id)
        for _run_id, thread in items:
            thread.join(timeout=5)
        for run_id, _thread in items:
            run = self.orchestrator.store.get_run(run_id)
            if run is not None and run.status is RunStatus.ACTIVE:
                with suppress(StoreError):
                    self.orchestrator.stop(run_id, "Agent worker shut down")

    def _run(self, run_id: str, lease_token: str) -> None:
        try:
            self.orchestrator.advance(run_id, Stage.IMPLEMENT, lease_token)
            routing = self.orchestrator.run_implementation_attempt(run_id, lease_token)
            attempt = routing.attempt
            if (
                attempt is not None
                and attempt.outcome is AttemptOutcome.SUCCESS
                and routing.state.status is RoutingStatus.RESOLVED
            ):
                self.orchestrator.store.complete_agent_run(
                    run_id,
                    routing.execution_result or "Bounded agent completed the demo",
                    lease_token,
                )
            else:
                failure = (
                    (attempt.failure_context if attempt is not None else "")
                    or routing.decision.failure_context
                    or ("Bounded agent did not complete the demo")
                )
                self.orchestrator.store.fail_agent_run(
                    run_id, redact_worker_text(failure), lease_token
                )
        except Exception as exc:
            run = self.orchestrator.store.get_run(run_id)
            if (
                run is not None
                and run.status is RunStatus.ACTIVE
                and run.lease_token == lease_token
            ):
                with suppress(StoreError):
                    self.orchestrator.store.fail_agent_run(
                        run_id,
                        redact_worker_text(f"Agent worker failed: {exc}"),
                        lease_token,
                    )
        finally:
            with self._lock:
                current = self._threads.get(run_id)
                if current is not None and current is current_thread():
                    self._threads.pop(run_id, None)


def _nonnegative_int(value: object, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _text_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for raw_item in cast(list[object], value):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, object], raw_item)
            if isinstance(item.get("text"), str):
                parts.append(_text_value(item.get("text")))
        return "\n".join(part for part in parts if part).strip()
    return ""
