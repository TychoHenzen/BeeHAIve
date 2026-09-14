from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .quality_gate_constants import (
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    PROCESS_SET_QUOTA,
    PROCESS_TERMINATE,
    WINDOWS_GATE_RUNNER,
)


def start_windows_gate_process(
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

    windows_ctypes = cast(Any, ctypes)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
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
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    process: subprocess.Popen[bytes] | None = None
    try:
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job_handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise windows_ctypes.WinError(windows_ctypes.get_last_error())
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", WINDOWS_GATE_RUNNER],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        process_handle = kernel32.OpenProcess(
            PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, process.pid
        )
        if not process_handle:
            raise windows_ctypes.WinError(windows_ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
                raise windows_ctypes.WinError(windows_ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process_handle)

        owned_job_handle = job_handle

        def close_job() -> None:
            nonlocal owned_job_handle
            if owned_job_handle:
                handle = owned_job_handle
                owned_job_handle = None
                if not kernel32.CloseHandle(handle):
                    raise windows_ctypes.WinError(windows_ctypes.get_last_error())

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
