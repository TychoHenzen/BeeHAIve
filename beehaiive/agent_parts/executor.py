from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from beehaiive.contracts import TaskContract
from beehaiive.workflow import WorkspaceLease

from .codex_command_mixin import CodexCommandMixin
from .codex_execution_mixin import CodexExecutionMixin
from .codex_process_mixin import CodexProcessMixin
from .codex_repair_execution_mixin import CodexRepairExecutionMixin
from .codex_repository_mixin import CodexRepositoryMixin
from .codex_runtime_mixin import CodexRuntimeMixin
from .codex_session_mixin import CodexSessionMixin
from .constants import (
    _SECRET_NAME,
    DEFAULT_DEMO_TASK,
    MAX_AGENT_TIMEOUT_SECONDS,
)
from .values import _nonnegative_int as _nonnegative_int
from .values import _text_value as _text_value
from .values import resolve_executable as resolve_executable
from .worker_text import redact_worker_text as redact_worker_text
from .worker_text import safe_worker_environment as safe_worker_environment


class CodexExecModelExecutor(
    CodexExecutionMixin,
    CodexRepairExecutionMixin,
    CodexRuntimeMixin,
    CodexCommandMixin,
    CodexRepositoryMixin,
    CodexProcessMixin,
    CodexSessionMixin,
):
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
        self.executable = resolve_executable(executable)
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
        self._session_tasks: dict[str, str] = {}
        self._session_event_handlers: dict[
            str, Callable[[str, str, str | None, str], None]
        ] = {}
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


__all__ = ["CodexExecModelExecutor"]
