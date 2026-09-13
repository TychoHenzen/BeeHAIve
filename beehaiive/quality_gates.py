"""Run the checks declared by one leased repository."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .agent import (
    MAX_AGENT_OUTPUT_LENGTH,
    CodexExecModelExecutor,
    redact_worker_text,
    safe_worker_environment,
    worker_secret_values,
)
from .workflow import CheckResult

MANIFEST_NAME = "beehaiive-gates.json"
MAX_MANIFEST_BYTES = 256_000
MAX_GATE_COUNT = 32
MAX_GATE_TIMEOUT_SECONDS = 900.0
_GATE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_CATEGORY = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ROOT_KEYS = {"version", "gates"}
_GATE_KEYS = {
    "name",
    "argv",
    "timeout_seconds",
    "category",
    "required",
    "external_only",
}
_WINDOWS_RUNNER_ERROR_PREFIX = "\x00BEEHAIIVE_GATE_RUNNER_ERROR:"
_WINDOWS_GATE_RUNNER = f"""
import json, subprocess, sys
spec = json.loads(sys.stdin.buffer.read())
try:
    result = subprocess.run(spec["argv"], stdin=subprocess.DEVNULL, check=False)
except (OSError, ValueError) as exc:
    sys.stderr.write({_WINDOWS_RUNNER_ERROR_PREFIX!r} + type(exc).__name__ + "\\x00")
    raise SystemExit(127)
raise SystemExit(result.returncode)
"""
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


@dataclass(frozen=True, slots=True)
class _Gate:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    category: str
    required: bool
    external_only: bool


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def _parse_manifest(value: object) -> tuple[_Gate, ...]:
    if not isinstance(value, dict):
        raise ValueError("Manifest must contain only version and gates")
    root = cast(dict[str, object], value)
    if set(root) != _ROOT_KEYS:
        raise ValueError("Manifest must contain only version and gates")
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("Manifest version must be 1")
    raw_gates = root.get("gates")
    if not isinstance(raw_gates, list):
        raise ValueError(f"Manifest must declare 1 to {MAX_GATE_COUNT} gates")
    gate_values = cast(list[object], raw_gates)
    if not 1 <= len(gate_values) <= MAX_GATE_COUNT:
        raise ValueError(f"Manifest must declare 1 to {MAX_GATE_COUNT} gates")

    gates: list[_Gate] = []
    names: set[str] = set()
    for index, gate_value in enumerate(gate_values):
        if not isinstance(gate_value, dict):
            raise ValueError(f"Gate {index + 1} has missing or unknown fields")
        raw_gate = cast(dict[str, object], gate_value)
        if set(raw_gate) != _GATE_KEYS:
            raise ValueError(f"Gate {index + 1} has missing or unknown fields")
        name = raw_gate.get("name")
        argv = raw_gate.get("argv")
        timeout = raw_gate.get("timeout_seconds")
        category = raw_gate.get("category")
        required = raw_gate.get("required")
        external_only = raw_gate.get("external_only")
        if not isinstance(name, str) or not _GATE_NAME.fullmatch(name):
            raise ValueError(f"Gate {index + 1} has an invalid name")
        if name in names:
            raise ValueError(f"Gate name is duplicated: {name}")
        names.add(name)
        if not isinstance(argv, list):
            raise ValueError(f"Gate {name} has an invalid argument vector")
        arguments = cast(list[object], argv)
        if len(arguments) > 64 or any(
            not isinstance(argument, str)
            or not argument.strip()
            or "\x00" in argument
            or len(argument) > 4_096
            for argument in arguments
        ):
            raise ValueError(f"Gate {name} has an invalid argument vector")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError(f"Gate {name} timeout must be a number")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as exc:
            raise ValueError(
                f"Gate {name} timeout exceeds the supported range"
            ) from exc
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_GATE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"Gate {name} timeout must be greater than 0 and no more than "
                f"{MAX_GATE_TIMEOUT_SECONDS:g} seconds"
            )
        if not isinstance(category, str) or not _CATEGORY.fullmatch(category):
            raise ValueError(f"Gate {name} has an invalid category")
        if type(required) is not bool or type(external_only) is not bool:
            raise ValueError(f"Gate {name} required and external_only must be booleans")
        required_value = required
        external_only_value = external_only
        if external_only_value:
            if arguments or required_value:
                raise ValueError(
                    f"External-only gate {name} must have an empty argv and be optional"
                )
        elif not arguments:
            raise ValueError(f"Local gate {name} needs an argument vector")
        gates.append(
            _Gate(
                name,
                tuple(cast(str, argument) for argument in arguments),
                timeout_seconds,
                category,
                required_value,
                external_only_value,
            )
        )
    return tuple(gates)


def _configuration_result(
    status: str, message: str, secret_values: tuple[str, ...]
) -> CheckResult:
    error = redact_worker_text(message, secret_values)
    return CheckResult(
        "gate_manifest",
        False,
        error,
        status=status,
        category="configuration",
        required=True,
        error=error,
    )


def _load_manifest(
    workspace: Path, secret_values: tuple[str, ...]
) -> tuple[_Gate, ...] | CheckResult:
    manifest = workspace / MANIFEST_NAME
    try:
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise ValueError(
                "Manifest must be a regular file inside the leased checkout"
            )
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        decoded = json.loads(manifest.read_bytes(), object_pairs_hook=_unique_object)
        return _parse_manifest(decoded)
    except FileNotFoundError:
        return _configuration_result(
            "configuration_missing",
            f"Required root manifest {MANIFEST_NAME} is missing",
            secret_values,
        )
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        message = (
            str(exc)
            if isinstance(exc, (ValueError, RecursionError))
            else "Manifest cannot be read"
        )
        return _configuration_result("invalid_configuration", message, secret_values)


def _redact(text: str, workspace: Path, secrets: tuple[str, ...]) -> str:
    root = str(workspace.resolve())
    for path in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
        text = text.replace(path, "<workspace>")
    return redact_worker_text(text, secrets, max_length=MAX_AGENT_OUTPUT_LENGTH)


def _start_windows_gate_process(
    argv: tuple[str, ...], workspace: Path, environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    process: subprocess.Popen[bytes] | None = None
    try:
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _WINDOWS_GATE_RUNNER],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        process_handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, process.pid
        )
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process_handle)

        owned_job_handle = job_handle

        def close_job() -> None:
            nonlocal owned_job_handle
            if owned_job_handle:
                handle = owned_job_handle
                owned_job_handle = None
                if not kernel32.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())

        cast(Any, process)._beehaiive_job_close = close_job
        job_handle = None
        if process.stdin is None:
            raise OSError("Gate runner input is unavailable")
        process.stdin.write(json.dumps({"argv": argv}).encode("utf-8"))
        process.stdin.close()
        return process
    except Exception:
        if process is not None:
            process_job_close = getattr(process, "_beehaiive_job_close", None)
            try:
                if callable(process_job_close):
                    process_job_close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        raise
    finally:
        if job_handle:
            kernel32.CloseHandle(job_handle)


class RepositoryGateSuite:
    """Load the leased repository's manifest and return every gate result."""

    def run(self, workspace: Path) -> tuple[CheckResult, ...]:
        secrets = worker_secret_values()
        gates = _load_manifest(workspace, secrets)
        if isinstance(gates, CheckResult):
            return (gates,)
        return tuple(self._run_gate(gate, workspace, secrets) for gate in gates)

    @staticmethod
    def _run_gate(
        gate: _Gate, workspace: Path, secrets: tuple[str, ...]
    ) -> CheckResult:
        argv = tuple(_redact(argument, workspace, secrets) for argument in gate.argv)
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
                process = _start_windows_gate_process(gate.argv, workspace, environment)
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
        runner_error_start = stderr.find(_WINDOWS_RUNNER_ERROR_PREFIX)
        runner_error = None
        if runner_error_start >= 0:
            runner_error_end = stderr.find(
                "\x00", runner_error_start + len(_WINDOWS_RUNNER_ERROR_PREFIX)
            )
            if runner_error_end < 0:
                runner_error_end = len(stderr)
            runner_error = stderr[
                runner_error_start
                + len(_WINDOWS_RUNNER_ERROR_PREFIX) : runner_error_end
            ]
            stderr = stderr[:runner_error_start] + stderr[runner_error_end + 1 :]
        stdout = _redact(stdout, workspace, secrets)
        stderr = _redact(stderr, workspace, secrets)
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
