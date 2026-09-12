"""Bounded local worker used by the dashboard demo."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread, current_thread
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from .contracts import ContractError, TaskContract, TaskOutcome, TaskResult
from .models import RunState, RunStatus, Stage
from .routing import (
    AttemptOutcome,
    ModelExecution,
    ModelExecutor,
    ModelSpec,
    RoutingDecision,
    RoutingStatus,
)
from .storage import StoreError
from .workflow import LeaseStatus, WorkflowError, WorkflowService, WorkspaceLease

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


DEMO_TASK_NAME = "bounded repository inventory"
DEFAULT_DEMO_TASK = (
    "Inspect only the current checkout and report the repository name, current "
    "branch, and count of tracked files. The runner supplies verified Git "
    "metadata because the isolated checkout omits .git. Confirm the copied "
    "files are present, then report that metadata. Do not edit files, create "
    "files, access the network, read credentials, or start other agents. Return "
    "a concise plain-text result."
)
MAX_AGENT_OUTPUT_LENGTH = 4_000
MAX_AGENT_OUTPUT_BYTES = 64_000
MAX_AGENT_TIMEOUT_SECONDS = 900.0
POST_TERMINATION_GRACE_SECONDS = 1.0
_SECRET_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY)", re.IGNORECASE
)
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "COLORTERM",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATHEXT",
        "PATH",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TERM_PROGRAM",
        "TMP",
        "USER",
        "USERPROFILE",
        "USERNAME",
        "WINDIR",
    }
)
_ROUTING_MODEL_ALIASES = frozenset({"luna", "terra", "sol", "astra", "human"})


class CancellableModelExecutor(ModelExecutor, Protocol):
    """Model executor capabilities required by the asynchronous worker."""

    def cancel(self, problem_id: str) -> None: ...

    def prepare_run(
        self,
        problem_id: str,
        repository: str,
        workspace_lease: WorkspaceLease | None = None,
        validate_workspace_lease: Callable[[], None] | None = None,
    ) -> None: ...

    def release_run(self, problem_id: str) -> None: ...


_SECRET_ASSIGNMENT = re.compile(
    r"\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_SECRET_JSON = re.compile(
    r"([\"']?(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|"
    r"client[_-]?secret|secret|password)[\"']?\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,}\s]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+:[^@\s]+@", re.IGNORECASE)


def redact_worker_text(text: str, secret_values: tuple[str, ...] = ()) -> str:
    """Remove common credential forms before worker text reaches durable state."""

    redacted = text
    for secret in sorted(
        (value for value in secret_values if value), key=len, reverse=True
    ):
        redacted = redacted.replace(secret, "[redacted]")
    redacted = _SECRET_JSON.sub(r"\1[redacted]", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]@", redacted)
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]", redacted
    )[:MAX_AGENT_OUTPUT_LENGTH]


class CodexExecModelExecutor:
    """Run one bounded, ephemeral ``codex exec`` process per routing attempt."""

    def __init__(
        self,
        repository: Path,
        *,
        executable: str = "codex",
        timeout_seconds: float = 120.0,
        model: str | None = None,
        task: str = DEFAULT_DEMO_TASK,
        repository_name: str | None = None,
    ) -> None:
        resolved_repository = repository.resolve()
        if not resolved_repository.is_dir():
            raise ValueError(f"Agent repository does not exist: {resolved_repository}")
        if not executable.strip():
            raise ValueError("Agent executable is required")
        if not math.isfinite(timeout_seconds) or not (
            0 < timeout_seconds <= MAX_AGENT_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Agent timeout must be positive, finite, and no greater than "
                f"{MAX_AGENT_TIMEOUT_SECONDS:g} seconds"
            )
        if not task.strip():
            raise ValueError("Agent task is required")
        self.repository = resolved_repository
        self.executable = executable.strip()
        self.timeout_seconds = timeout_seconds
        self.model = model.strip() if model and model.strip() else None
        self.task = task.strip()
        self.repository_name = (
            repository_name.strip()
            if repository_name and repository_name.strip()
            else self._discover_repository_name()
        )
        self._lock = Lock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._active_attempts: set[str] = set()
        self._cancelled: set[str] = set()
        self._task_contracts: dict[str, TaskContract] = {}
        self._workspace_leases: dict[str, WorkspaceLease] = {}
        self._workspace_validators: dict[str, Callable[[], None]] = {}
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
            repository_name=os.environ.get("BEEHAIIVE_AGENT_REPOSITORY_NAME"),
        )

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        """Run the fixed safe task and return only its final agent message."""

        problem_id = decision.problem_id
        with self._lock:
            contract = self._task_contracts.get(problem_id)
            workspace_lease = self._workspace_leases.get(problem_id)
            validate_workspace_lease = self._workspace_validators.get(problem_id)
        execution_repository = (
            None if workspace_lease is None else Path(workspace_lease.worktree_path)
        )
        if workspace_lease is not None:
            try:
                if validate_workspace_lease is None:
                    raise WorkflowError("Workspace lease validator is missing")
                validate_workspace_lease()
            except (StoreError, WorkflowError) as exc:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context=f"Workflow lease validation failed: {exc}",
                )
            if execution_repository is None or not execution_repository.is_dir():
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Leased worker worktree does not exist",
                )
        prompt = self._prompt(
            spec, decision, contract, execution_repository, workspace_lease is not None
        )
        with self._lock:
            self._active_attempts.add(problem_id)
            if problem_id in self._cancelled:
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
        process: subprocess.Popen[str] | None = None
        try:
            checkout = (
                self._safe_checkout()
                if workspace_lease is None
                else nullcontext(execution_repository)
            )
            with checkout as execution_repository:
                assert execution_repository is not None
                command = (
                    self._command_for_execution(
                        prompt, spec.model, execution_repository
                    )
                    if workspace_lease is None
                    else self._workspace_command_for_execution(
                        prompt, spec.model, execution_repository
                    )
                )
                process = self._start_process(
                    command,
                    self._safe_environment(),
                )
                with self._lock:
                    self._processes[problem_id] = process
                    cancelled = problem_id in self._cancelled
                if cancelled:
                    self._terminate_process(process)
                try:
                    stdout, stderr, timed_out = self._communicate_bounded(
                        process, self.timeout_seconds
                    )
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    stdout, stderr = "", ""
                    timed_out = True
                if validate_workspace_lease is not None:
                    try:
                        validate_workspace_lease()
                    except (StoreError, WorkflowError) as exc:
                        return ModelExecution(
                            AttemptOutcome.FAILURE,
                            failure_context=f"Workflow lease validation failed: {exc}",
                        )
                if timed_out:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        failure_context=(
                            f"Agent timed out after {self.timeout_seconds:g} seconds"
                        ),
                    )
                with self._lock:
                    was_cancelled = problem_id in self._cancelled
                if was_cancelled:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        failure_context="Agent stopped by operator",
                    )

                result, input_tokens, output_tokens = self._parse_output(stdout)
                result = redact_worker_text(result, self._secret_values).strip()
                if process.returncode != 0:
                    detail = redact_worker_text(
                        stderr or stdout, self._secret_values
                    ).strip()
                    if not detail:
                        detail = f"codex exec exited with status {process.returncode}"
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        failure_context=f"Bounded agent failed: {detail}",
                        task_result=(
                            TaskResult.invalid(detail) if contract is not None else None
                        ),
                    )
                if not result:
                    return ModelExecution(
                        AttemptOutcome.FAILURE,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        failure_context="Bounded agent returned no result",
                        task_result=(
                            TaskResult.invalid("Bounded agent returned no result")
                            if contract is not None
                            else None
                        ),
                    )
                task_result = None
                if contract is not None:
                    try:
                        task_result = TaskResult.from_payload(
                            json.loads(result), contract
                        )
                    except (ContractError, json.JSONDecodeError) as exc:
                        reason = f"Invalid structured task result: {exc}"
                        return ModelExecution(
                            AttemptOutcome.SUCCESS,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            failure_context=reason,
                            task_result=TaskResult.invalid(reason),
                        )
                return ModelExecution(
                    AttemptOutcome.SUCCESS,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    result=result,
                    task_result=task_result,
                )
        finally:
            with self._lock:
                if process is not None:
                    self._processes.pop(problem_id, None)
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)

    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        """Run one bounded write-enabled repair inside the leased worktree."""

        if not worktree.is_dir():
            return ModelExecution(
                AttemptOutcome.FAILURE,
                failure_context="Repair worktree does not exist",
            )
        prompt = (
            "BeeHAIve conflict repair.\n"
            f"Source branch: {source_branch}\n"
            f"Target branch: {target_branch}\n"
            "This is the exact leased repair worktree. Resolve every merge conflict "
            "left by the target integration. Work only inside this worktree. Do not "
            "push, force-push, access credentials, edit another checkout, or start "
            "another agent. Run the configured checks only when useful. Stage the "
            "resolved files and create one normal merge commit. Return a concise "
            "plain-text result after the commit succeeds."
        )
        with self._lock:
            self._active_attempts.add(problem_id)
            if problem_id in self._cancelled:
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
        process: subprocess.Popen[str] | None = None
        try:
            process = self._start_process(
                self._repair_command(prompt, worktree), self._safe_environment()
            )
            with self._lock:
                self._processes[problem_id] = process
                cancelled = problem_id in self._cancelled
            if cancelled:
                self._terminate_process(process)
            try:
                stdout, stderr, timed_out = self._communicate_bounded(
                    process, self.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                stdout, stderr, timed_out = "", "", True
            if timed_out:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context=(
                        f"Repair agent timed out after {self.timeout_seconds:g} seconds"
                    ),
                )
            with self._lock:
                was_cancelled = problem_id in self._cancelled
            if was_cancelled:
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    failure_context="Agent stopped by operator",
                )
            result, input_tokens, output_tokens = self._parse_output(stdout)
            result = redact_worker_text(result, self._secret_values).strip()
            if process.returncode != 0:
                detail = redact_worker_text(stderr or stdout, self._secret_values)
                return ModelExecution(
                    AttemptOutcome.FAILURE,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    failure_context=(
                        "Bounded repair agent failed: "
                        f"{detail.strip() or 'process failed'}"
                    ),
                )
            return ModelExecution(
                AttemptOutcome.SUCCESS,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                result=result or "Conflict repair agent completed",
            )
        finally:
            with self._lock:
                if process is not None:
                    self._processes.pop(problem_id, None)
                self._cancelled.discard(problem_id)
                self._active_attempts.discard(problem_id)

    def cancel(self, problem_id: str) -> None:
        """Terminate the process for one run, including its child process tree."""

        with self._lock:
            process = self._processes.get(problem_id)
            if problem_id in self._active_attempts or process is not None:
                self._cancelled.add(problem_id)
        if process is not None:
            self._terminate_process(process)

    def prepare_run(
        self,
        problem_id: str,
        repository: str,
        workspace_lease: WorkspaceLease | None = None,
        validate_workspace_lease: Callable[[], None] | None = None,
    ) -> None:
        self.validate_repository(repository)
        if workspace_lease is not None:
            if workspace_lease.status is not LeaseStatus.ACTIVE:
                raise StoreError("An active workflow workspace lease is required")
            if not workspace_lease.lease_token or validate_workspace_lease is None:
                raise StoreError("A workflow workspace lease token is required")
            worktree = Path(workspace_lease.worktree_path)
            if not worktree.is_absolute() or not worktree.is_dir():
                raise StoreError("The leased worker worktree is unavailable")
            unsafe_files = tuple(
                relative
                for relative in self._repository_files()
                if not self._is_safe_repository_file(relative)
            )
            if unsafe_files:
                raise StoreError(
                    "Credential-like tracked files cannot enter a worker worktree"
                )
            validate_workspace_lease()
        with self._lock:
            self._active_attempts.add(problem_id)
            if workspace_lease is not None:
                self._workspace_leases[problem_id] = workspace_lease
                self._workspace_validators[problem_id] = cast(
                    Callable[[], None], validate_workspace_lease
                )

    def build_task_contract(self, run: RunState) -> TaskContract:
        repository = self._execution_repository_for(run.run_id)
        return TaskContract.inventory(
            run.repository,
            run.pbi_number,
            run.title,
            branch=self._discover_repository_branch(repository),
            tracked_file_count=len(self._repository_files(repository)),
            answer=run.task_answer,
        )

    def set_task_contract(self, problem_id: str, contract: TaskContract) -> None:
        contract.validate()
        with self._lock:
            self._task_contracts[problem_id] = contract

    def release_run(self, problem_id: str) -> None:
        with self._lock:
            self._active_attempts.discard(problem_id)
            self._cancelled.discard(problem_id)
            self._task_contracts.pop(problem_id, None)
            self._workspace_leases.pop(problem_id, None)
            self._workspace_validators.pop(problem_id, None)

    def _execution_repository_for(self, problem_id: str) -> Path:
        with self._lock:
            lease = self._workspace_leases.get(problem_id)
        return self.repository if lease is None else Path(lease.worktree_path)

    def validate_repository(self, repository: str) -> None:
        if self.repository_name is None:
            raise StoreError(
                "Agent repository identity is not configured. Set "
                "BEEHAIIVE_AGENT_REPOSITORY_NAME."
            )
        if repository != self.repository_name:
            raise StoreError(
                f"Agent checkout {self.repository_name!r} does not match "
                f"claimed repository {repository!r}"
            )

    def _prompt(
        self,
        spec: ModelSpec,
        decision: RoutingDecision,
        contract: TaskContract | None = None,
        repository: Path | None = None,
        workspace_write: bool = False,
    ) -> str:
        branch = (
            self._discover_repository_branch()
            if repository is None
            else self._discover_repository_branch(repository)
        )
        tracked_file_count = len(
            self._repository_files()
            if repository is None
            else self._repository_files(repository)
        )
        scope = (
            "The exact leased worktree is the only writable path. Work only inside "
            "it. Do not access the network or credentials, modify another checkout, "
            "start other agents, commit, or push."
            if workspace_write
            else "The task is read-only. Do not report success unless the inspection "
            "completed."
        )
        prompt = (
            "BeeHAIve dashboard demo.\n"
            f"Task name: {DEMO_TASK_NAME}\n"
            f"Task: {self.task}\n"
            f"Repository identity: {self.repository_name or self.repository.name}\n"
            f"Verified current branch: {branch}\n"
            f"Verified tracked file count: {tracked_file_count}\n"
            f"Routing tier: {spec.tier.value}. Routing reason: {decision.reason}.\n"
            f"{scope}"
        )
        if contract is not None:
            prompt += (
                "\nThe following versioned task contract is authoritative. Return "
                "exactly one JSON object with outcome, evidence, artifact_refs, "
                "question, required_action, and validation_reason. Always include "
                "evidence as an object and artifact_refs as an array, even when "
                "empty. Use null for unused optional fields. Do not include answer. "
                "Add no markdown fences or extra text.\n"
                f"{json.dumps(contract.as_dict(), sort_keys=True)}"
            )
        return prompt

    def _command(
        self,
        prompt: str,
        model: str | None = None,
        repository: Path | None = None,
        *,
        workspace_write: bool = False,
    ) -> list[str]:
        selected_model = model.strip() if model and model.strip() else self.model
        if self.model is None and selected_model in _ROUTING_MODEL_ALIASES:
            selected_model = None
        command = [
            self.executable,
            "--approve-for-me",
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
        ]
        if workspace_write:
            command.extend(
                (
                    "--strict-config",
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                    "-c",
                    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-c",
                    "agents.enabled=false",
                )
            )
        command.extend(
            [
                "--sandbox",
                "workspace-write" if workspace_write else "read-only",
                "--cd",
                str(repository or self.repository),
            ]
        )
        if selected_model is not None:
            command.extend(("--model", selected_model))
        command.append(prompt)
        return command

    def _command_for_execution(
        self, prompt: str, model: str, repository: Path
    ) -> list[str]:
        if type(self)._command is not CodexExecModelExecutor._command:
            return self._command(prompt)
        return self._command(prompt, model, repository)

    def _workspace_command_for_execution(
        self, prompt: str, model: str, repository: Path
    ) -> list[str]:
        return CodexExecModelExecutor._command(
            self, prompt, model, repository, workspace_write=True
        )

    def _repair_command(self, prompt: str, repository: Path) -> list[str]:
        selected_model = self.model
        command = [
            self.executable,
            "--approve-for-me",
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(repository),
        ]
        if selected_model is not None:
            command.extend(("--model", selected_model))
        command.append(prompt)
        return command

    def _safe_environment(self) -> dict[str, str]:
        return {
            name: value
            for name, value in os.environ.items()
            if name.upper() in _SAFE_ENVIRONMENT_NAMES
        }

    @contextmanager
    def _safe_checkout(self) -> Generator[Path]:
        """Give the child only a temporary copy without local credentials."""

        with TemporaryDirectory(prefix="beehaiive-agent-") as directory:
            destination = Path(directory)
            for relative in self._repository_files():
                if not self._is_safe_repository_file(relative):
                    continue
                source = (self.repository / relative).resolve()
                try:
                    source.relative_to(self.repository)
                except ValueError:
                    continue
                if not source.is_file() or source.is_symlink():
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            yield destination

    def _repository_files(self, repository: Path | None = None) -> tuple[Path, ...]:
        source = self.repository if repository is None else repository.resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "ls-files", "-z"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            return tuple(
                Path(os.fsdecode(value))
                for value in result.stdout.split(b"\0")
                if value
            )
        return tuple(
            path.relative_to(source)
            for path in source.rglob("*")
            if path.is_file()
            and not any(
                part in {".git", ".beehaiive", "__pycache__"} for part in path.parts
            )
        )

    @staticmethod
    def _is_safe_repository_file(relative: Path) -> bool:
        name = relative.name.lower()
        return not (
            name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
            or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
            or any(
                marker in name
                for marker in (
                    "secret",
                    "credential",
                    "password",
                    "token",
                    "api-key",
                    "api_key",
                    "apikey",
                    "private-key",
                    "private_key",
                    "service-account",
                    "service_account",
                )
            )
            or relative.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
        )

    def _discover_repository_name(self) -> str | None:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "config",
                    "--get",
                    "remote.origin.url",
                ],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        remote = result.stdout.strip()
        match = re.search(r"([^/:\s]+/[^/\s]+?)(?:\.git)?$", remote)
        return match.group(1) if match else None

    def _discover_repository_branch(self, repository: Path | None = None) -> str:
        source = self.repository if repository is None else repository.resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "branch", "--show-current"],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return result.stdout.strip() or "unknown"

    @staticmethod
    def _start_process(
        command: list[str], environment: dict[str, str]
    ) -> subprocess.Popen[Any]:
        common: dict[str, Any] = {
            "cwd": str(Path(command[command.index("--cd") + 1])),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
        }
        if os.name == "nt":
            return subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                **common,
            )
        return subprocess.Popen(command, start_new_session=True, **common)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any]) -> None:
        running = process.poll() is None
        if os.name == "nt":
            command = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=3,
            )
            if running:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                        timeout=3,
                    )
                    process.wait(timeout=3)
            return

        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            if running:
                process.terminate()

        deadline = time.monotonic() + 3
        if running:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=max(0, deadline - time.monotonic()))
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        else:
            with suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)

        if not running:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)

    @staticmethod
    def _communicate_bounded(
        process: subprocess.Popen[Any], timeout: float
    ) -> tuple[str, str, bool]:
        buffers = [bytearray(), bytearray()]
        streams = [process.stdout, process.stderr]

        def collect(stream: Any, buffer: bytearray) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", errors="replace")
                    remaining = MAX_AGENT_OUTPUT_BYTES - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
            except (OSError, ValueError):
                return

        readers = [
            Thread(
                target=collect,
                args=(stream, buffer),
                name="beehaiive-agent-output",
                daemon=True,
            )
            for stream, buffer in zip(streams, buffers, strict=True)
            if stream is not None
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            CodexExecModelExecutor._terminate_process(process)
        for reader, stream in zip(
            readers, (stream for stream in streams if stream is not None), strict=True
        ):
            reader.join(timeout=POST_TERMINATION_GRACE_SECONDS)
            if reader.is_alive():
                with suppress(OSError, ValueError):
                    stream.close()
        return (
            buffers[0].decode("utf-8", errors="replace"),
            buffers[1].decode("utf-8", errors="replace"),
            timed_out,
        )

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
        self,
        orchestrator: Orchestrator,
        executor: CancellableModelExecutor,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.executor = executor
        self.workflow_service = workflow_service
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._workspace_leases: dict[str, WorkspaceLease] = {}
        self._workspace_validators: dict[str, Callable[[], None]] = {}
        if workflow_service is not None:
            workflow_service.cleanup_dashboard_run_workspaces()
        register = getattr(orchestrator, "register_worker_canceller", None)
        if callable(register):
            register(self.cancel)

    def start(self, run: RunState) -> None:
        if run.status is not RunStatus.ACTIVE or run.lease_token is None:
            raise StoreError("An active leased run is required")
        if self.workflow_service is None:
            raise StoreError("Workflow service is required for a dashboard worker")
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
        workspace_lease: WorkspaceLease | None = None
        try:
            service_repository = self.workflow_service.worktrees.repository
            executor_repository = getattr(
                self.executor, "repository", service_repository
            )
            if Path(executor_repository).resolve() != service_repository:
                raise StoreError(
                    "Agent executor and workflow service must use the same repository"
                )
            workspace_root = (
                service_repository.parent
                / f".{service_repository.name}.beehaiive"
                / "agent-worktrees"
            )
            workspace_root.mkdir(parents=True, exist_ok=True)
            workspace_id = uuid4().hex
            workspace_lease = self.workflow_service.acquire_workspace(
                f"dashboard-run:{run.run_id}",
                f"codex/beehaiive-run-{workspace_id}",
                workspace_root / workspace_id,
            )
            validate_workspace_lease = self._workspace_validator(
                run.run_id, workspace_lease
            )
            self.executor.prepare_run(
                run.run_id,
                run.repository,
                workspace_lease,
                validate_workspace_lease,
            )
            with self._lock:
                self._workspace_leases[run.run_id] = workspace_lease
                self._workspace_validators[run.run_id] = validate_workspace_lease
            thread.start()
        except Exception as exc:
            with self._lock:
                self._threads.pop(run.run_id, None)
                self._workspace_leases.pop(run.run_id, None)
                self._workspace_validators.pop(run.run_id, None)
            with suppress(Exception):
                self.executor.release_run(run.run_id)
            if workspace_lease is not None:
                try:
                    self.workflow_service.discard_workspace(
                        workspace_lease.lease_id, "Dashboard worker failed to start"
                    )
                except WorkflowError as cleanup_error:
                    raise StoreError(
                        "Dashboard worker failed to start and workspace cleanup "
                        f"failed: {cleanup_error}"
                    ) from exc
            raise

    def _workspace_validator(
        self, run_id: str, expected: WorkspaceLease
    ) -> Callable[[], None]:
        service = self.workflow_service
        if service is None:
            raise WorkflowError("Workflow service is required for a dashboard worker")

        def validate() -> None:
            current = service.store.require_lease_token(
                expected.lease_id, expected.lease_token
            )
            if (
                current.agent_id != f"dashboard-run:{run_id}"
                or current.worktree_path != expected.worktree_path
                or current.lease_token != expected.lease_token
            ):
                raise WorkflowError("Dashboard worker workspace lease changed")

        return validate

    def cancel(self, run_id: str) -> None:
        self.executor.cancel(run_id)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self._threads.items())
        cancellation_error: Exception | None = None
        for run_id, _thread in items:
            try:
                self.cancel(run_id)
            except Exception as exc:
                if cancellation_error is None:
                    cancellation_error = exc
        for _run_id, thread in items:
            thread.join(timeout=5)
            is_alive = getattr(thread, "is_alive", lambda: False)
            if is_alive():
                thread.join(timeout=1)
        live_workers = [
            run_id
            for run_id, thread in items
            if getattr(thread, "is_alive", lambda: False)()
        ]
        if cancellation_error is not None:
            raise cancellation_error
        if live_workers:
            raise StoreError(
                "Agent workers did not stop before shutdown: " + ", ".join(live_workers)
            )
        for run_id, _thread in items:
            run = self.orchestrator.store.get_run(run_id)
            if run is not None and run.status is RunStatus.ACTIVE:
                with suppress(StoreError):
                    self.orchestrator.stop(run_id, "Agent worker shut down")
        if self.workflow_service is not None:
            self.workflow_service.cleanup_dashboard_run_workspaces()

    def _run(self, run_id: str, lease_token: str) -> None:
        workflow_service = self.workflow_service
        with self._lock:
            workspace_lease = self._workspace_leases.get(run_id)
            validate_workspace_lease = self._workspace_validators.get(run_id)
        heartbeat_stop = Event()
        heartbeat_errors: list[Exception] = []
        heartbeat_thread: Thread | None = None

        if workspace_lease is not None and workflow_service is not None:

            def heartbeat() -> None:
                heartbeat_seconds = workflow_service.store.lease_heartbeat_seconds
                while not heartbeat_stop.wait(heartbeat_seconds):
                    try:
                        workflow_service.store.renew_lease(
                            workspace_lease.lease_id, workspace_lease.lease_token
                        )
                    except Exception as exc:
                        heartbeat_errors.append(exc)
                        self.cancel(run_id)
                        return

            heartbeat_thread = Thread(
                target=heartbeat, name=f"beehaiive-lease-{run_id[:8]}", daemon=True
            )
            heartbeat_thread.start()
        preserve_workspace = False
        try:
            self.orchestrator.advance(run_id, Stage.IMPLEMENT, lease_token)
            if validate_workspace_lease is not None:
                validate_workspace_lease()
            routing = self.orchestrator.run_implementation_attempt(run_id, lease_token)
            heartbeat_stop.set()
            if heartbeat_thread is not None and workflow_service is not None:
                heartbeat_thread.join(
                    timeout=max(workflow_service.store.lease_heartbeat_seconds, 1.0)
                )
                if heartbeat_thread.is_alive():
                    raise WorkflowError("Workflow lease heartbeat did not stop")
            if heartbeat_errors:
                raise WorkflowError(
                    f"Workflow workspace lease was lost: {heartbeat_errors[0]}"
                )
            if validate_workspace_lease is not None:
                validate_workspace_lease()
            attempt = routing.attempt
            if routing.state.status is RoutingStatus.HUMAN_HANDOFF:
                task_result = getattr(routing, "task_result", None)
                failure = (
                    task_result.question
                    if isinstance(task_result, TaskResult)
                    and task_result.outcome is TaskOutcome.QUESTION
                    else task_result.required_action
                    if isinstance(task_result, TaskResult)
                    and task_result.outcome is TaskOutcome.BLOCKED
                    else routing.state.required_action or "Human action required"
                )
                self.orchestrator.store.fail_agent_run(
                    run_id,
                    redact_worker_text(failure or "Human action required"),
                    lease_token,
                    claimable=False,
                )
            elif (
                attempt is not None
                and attempt.outcome is AttemptOutcome.SUCCESS
                and routing.state.status is RoutingStatus.RESOLVED
            ):
                try:
                    if workspace_lease is not None and workflow_service is not None:
                        workflow_service.retain_workspace(
                            workspace_lease.lease_id, workspace_lease.lease_token
                        )
                        preserve_workspace = True
                    self.orchestrator.store.complete_agent_run(
                        run_id,
                        routing.execution_result or "Bounded agent completed the demo",
                        lease_token,
                    )
                except Exception as exc:
                    reopen = getattr(self.orchestrator, "recover_routing_problem", None)
                    if callable(reopen):
                        reopen(run_id, f"Run result persistence failed: {exc}")
                    raise
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
                failure = redact_worker_text(f"Agent worker failed: {exc}")
                try:
                    self.orchestrator.store.fail_agent_run(run_id, failure, lease_token)
                except StoreError:
                    recover = getattr(
                        self.orchestrator.store, "fail_agent_run_after_lease_loss", None
                    )
                    if callable(recover):
                        recover(run_id, failure)
        finally:
            heartbeat_stop.set()
            cleanup_error: WorkflowError | None = None
            if (
                heartbeat_thread is not None
                and workflow_service is not None
                and heartbeat_thread.is_alive()
            ):
                heartbeat_thread.join(
                    timeout=max(workflow_service.store.lease_heartbeat_seconds, 1.0)
                )
            if (
                workspace_lease is not None
                and workflow_service is not None
                and not preserve_workspace
            ):
                try:
                    workflow_service.discard_workspace(
                        workspace_lease.lease_id, "Dashboard worker did not complete"
                    )
                except WorkflowError as exc:
                    cleanup_error = exc
            self.executor.release_run(run_id)
            with self._lock:
                self._workspace_leases.pop(run_id, None)
                self._workspace_validators.pop(run_id, None)
                current = self._threads.get(run_id)
                if current is not None and current is current_thread():
                    self._threads.pop(run_id, None)
            if (
                workspace_lease is not None
                and workflow_service is not None
                and not preserve_workspace
                and cleanup_error is not None
            ):
                raise StoreError(f"Worker workspace cleanup failed: {cleanup_error}")


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
