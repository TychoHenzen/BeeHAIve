from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent import (
    CodexExecModelExecutor,
    redact_worker_text,
    safe_worker_environment,
    worker_secret_values,
)
from ..workflow import CheckResult
from .quality_gate_constants import WINDOWS_RUNNER_ERROR_PREFIX
from .quality_gate_helpers import load_manifest, redact
from .quality_gate_process import start_windows_gate_process

if TYPE_CHECKING:
    from .gate_definition import GateDefinition


class RepositoryGateSuite:
    """Load the leased repository's manifest and return every gate result."""

    def run(self, workspace: Path) -> tuple[CheckResult, ...]:
        secrets = worker_secret_values()
        gates = load_manifest(workspace, secrets)
        if isinstance(gates, CheckResult):
            return (gates,)
        return tuple(self._run_gate(gate, workspace, secrets) for gate in gates)

    @staticmethod
    def _run_gate(
        gate: GateDefinition, workspace: Path, secrets: tuple[str, ...]
    ) -> CheckResult:
        argv = tuple(redact(argument, workspace, secrets) for argument in gate.argv)
        if gate.external_only:
            message = "Available only through the repository's external CI checks"
            return CheckResult(
                gate.name,
                False,
                message,
                status="external_only",
                category=gate.category,
                required=False,
                argv=argv,
                error=message,
            )

        try:
            environment = safe_worker_environment()
            common: dict[str, Any] = {
                "cwd": workspace,
                "env": environment,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
            }
            if os.name == "nt":
                process = start_windows_gate_process(gate.argv, workspace, environment)
            else:
                process = subprocess.Popen(gate.argv, start_new_session=True, **common)
        except FileNotFoundError:
            error = f"Executable not found: {argv[0]}"
            return CheckResult(
                gate.name,
                False,
                error,
                status="unavailable",
                category=gate.category,
                required=gate.required,
                argv=argv,
                error=error,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = f"Executable could not be started: {type(exc).__name__}"
            return CheckResult(
                gate.name,
                False,
                error,
                status="unavailable",
                category=gate.category,
                required=gate.required,
                argv=argv,
                error=error,
            )

        try:
            stdout, stderr, timed_out = CodexExecModelExecutor.communicate_bounded(
                process,
                gate.timeout_seconds,
                terminate_descendants=True,
            )
        finally:
            CodexExecModelExecutor._terminate_process(  # pyright: ignore[reportPrivateUsage]
                process
            )
        runner_error_start = stderr.find(WINDOWS_RUNNER_ERROR_PREFIX)
        runner_error = None
        if runner_error_start >= 0:
            runner_error_end = stderr.find(
                "\x00", runner_error_start + len(WINDOWS_RUNNER_ERROR_PREFIX)
            )
            if runner_error_end < 0:
                runner_error_end = len(stderr)
            runner_error = stderr[
                runner_error_start + len(WINDOWS_RUNNER_ERROR_PREFIX) : runner_error_end
            ]
            stderr = stderr[:runner_error_start] + stderr[runner_error_end + 1 :]
        stdout = redact(stdout, workspace, secrets)
        stderr = redact(stderr, workspace, secrets)
        exit_code = None if runner_error else process.returncode
        passed = not timed_out and runner_error is None and exit_code == 0
        status = (
            "timed_out"
            if timed_out
            else "unavailable"
            if runner_error
            else "passed"
            if passed
            else "failed"
        )
        if runner_error:
            error = f"Executable could not be started: {argv[0]}"
        elif timed_out:
            error = f"Gate exceeded its {gate.timeout_seconds:g}-second timeout"
        elif passed:
            error = None
        else:
            error = f"Command exited with status {exit_code}"
        evidence = error or "Gate passed"
        if error is not None:
            error = redact_worker_text(error, secrets)
        return CheckResult(
            gate.name,
            passed,
            evidence,
            status=status,
            category=gate.category,
            required=gate.required,
            argv=argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=error,
        )
