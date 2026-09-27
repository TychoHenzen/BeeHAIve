from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


class _BasicLimitInformation(ctypes.Structure):
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


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def run_in_job(command: list[str]) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create_job.restype = wintypes.HANDLE
    set_limits = kernel32.SetInformationJobObject
    set_limits.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    set_limits.restype = wintypes.BOOL
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    job = create_job(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    child: subprocess.Popen[bytes] | None = None
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not set_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        child = subprocess.Popen(command)
        process = open_process(0x0101, False, child.pid)
        if not process:
            child.terminate()
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not assign(job, process):
                child.terminate()
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(process)
        try:
            return child.wait()
        except KeyboardInterrupt:
            return 130
    finally:
        close_handle(job)
        if child is not None:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("The dashboard batch launcher requires Windows")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError:
            print(
                "Port 8000 is already in use; close the existing dashboard first.",
                file=sys.stderr,
            )
            return 1

    root = Path.cwd()
    project = (
        f"{os.environ['GITHUB_PROJECT_OWNER']}:{os.environ['GITHUB_PROJECT_NUMBER']}"
    )
    print(
        f"BeeHAIve dashboard: http://127.0.0.1:8000/dashboard?project={project}",
        flush=True,
    )
    print("Documentation: http://127.0.0.1:8000/docs", flush=True)
    print("Stop the server with Ctrl+C.", flush=True)
    return run_in_job(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--app-dir",
            str(root),
            "--reload",
            "--reload-dir",
            str(root),
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
