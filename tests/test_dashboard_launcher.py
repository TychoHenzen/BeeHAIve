from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from beehaiive.dashboard_launcher import run_in_job


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_dashboard_job_closes_spawned_worker_on_exit(tmp_path: Path) -> None:
    worker_id_file = tmp_path / "worker-id"
    command = (
        "import subprocess,sys;"
        "worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"open({str(worker_id_file)!r},'w').write(str(worker.pid))"
    )

    assert run_in_job([sys.executable, "-c", command]) == 0
    worker_id = int(worker_id_file.read_text(encoding="utf-8"))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    terminate = kernel32.TerminateProcess
    terminate.argtypes = (wintypes.HANDLE, wintypes.UINT)
    terminate.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    process = open_process(0x00100001, False, worker_id)
    if not process:
        assert ctypes.get_last_error() == 87
        return
    try:
        result = wait(process, 5000)
        if result != 0:
            terminate(process, 1)
        assert result == 0
    finally:
        close_handle(process)
