from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Thread
from typing import Any

from .constants import MAX_AGENT_OUTPUT_BYTES, POST_TERMINATION_GRACE_SECONDS


class CodexProcessMixin:
    @staticmethod
    def _start_process(
        command: list[str], environment: dict[str, str]
    ) -> subprocess.Popen[Any]:
        cwd = str(Path(command[command.index("--cd") + 1]))
        common: dict[str, Any] = {
            "cwd": cwd,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
        }
        try:
            if os.name == "nt":
                return subprocess.Popen(
                    command,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    **common,
                )
            return subprocess.Popen(command, start_new_session=True, **common)
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={command[0]!r}; cwd={cwd!r}; "
                f"filename={error.filename!r}; errno={error.errno}; "
                f"message={error.strerror or str(error)}"
            ) from error
        except OSError as error:
            raise RuntimeError(
                f"Codex launch failed: executable={command[0]!r}; cwd={cwd!r}; "
                f"errno={error.errno}; message={error.strerror or str(error)}"
            ) from error

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any]) -> None:
        running = process.poll() is None
        if os.name == "nt":
            close_job = getattr(process, "_beehaiive_job_close", None)
            if callable(close_job):
                close_job()
                if running:
                    process.wait(timeout=3)
                return
            command = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
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
                        check=False,
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
    def communicate_bounded(
        process: subprocess.Popen[Any],
        timeout: float | None,
        *,
        terminate_descendants: bool = False,
    ) -> tuple[str, str, bool]:
        """Capture bounded stdout and stderr while enforcing the process timeout."""

        from .executor import CodexExecModelExecutor

        return CodexExecModelExecutor._communicate_bounded(
            process, timeout, terminate_descendants=terminate_descendants
        )

    @staticmethod
    def _communicate_bounded(
        process: subprocess.Popen[Any],
        timeout: float | None,
        stdout_line_handler: Callable[[str], None] | None = None,
        *,
        stderr_line_handler: Callable[[str], None] | None = None,
        capture_gap_handler: Callable[[str], None] | None = None,
        terminate_descendants: bool = False,
    ) -> tuple[str, str, bool]:
        from .executor import CodexExecModelExecutor

        buffers = [bytearray(), bytearray()]
        streams = [process.stdout, process.stderr]
        handler_errors: list[Exception] = []

        def capture_gap(reason: str) -> None:
            if capture_gap_handler is not None and not handler_errors:
                try:
                    capture_gap_handler(reason)
                except Exception as exc:
                    handler_errors.append(exc)

        def collect(
            stream: Any,
            buffer: bytearray,
            line_handler: Callable[[str], None] | None,
        ) -> None:
            line_buffer = bytearray()
            dropping_line = False

            def handle_lines(chunk: bytes) -> None:
                nonlocal dropping_line
                if dropping_line:
                    newline = chunk.find(b"\n")
                    if newline < 0:
                        return
                    chunk = chunk[newline + 1 :]
                    dropping_line = False
                line_buffer.extend(chunk)
                while True:
                    newline = line_buffer.find(b"\n")
                    if newline < 0:
                        if len(line_buffer) > MAX_AGENT_OUTPUT_BYTES:
                            capture_gap("oversized")
                            line_buffer.clear()
                            dropping_line = True
                        return
                    line = bytes(line_buffer[:newline])
                    del line_buffer[: newline + 1]
                    if len(line) > MAX_AGENT_OUTPUT_BYTES:
                        capture_gap("oversized")
                        continue
                    if handler_errors:
                        continue
                    try:
                        assert line_handler is not None
                        line_handler(line.decode("utf-8", errors="replace"))
                    except Exception as exc:
                        handler_errors.append(exc)

            try:
                while True:
                    chunk = stream.read1(4096)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", errors="replace")
                    remaining = MAX_AGENT_OUTPUT_BYTES - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if line_handler is not None and not handler_errors:
                        handle_lines(chunk)
                if (
                    line_handler is not None
                    and line_buffer
                    and not dropping_line
                    and not handler_errors
                ):
                    try:
                        line_handler(line_buffer.decode("utf-8", errors="replace"))
                    except Exception as exc:
                        handler_errors.append(exc)
            except (OSError, ValueError):
                capture_gap("interrupted")
                return

        readers = [
            Thread(
                target=collect,
                args=(
                    stream,
                    buffer,
                    stdout_line_handler if index == 0 else stderr_line_handler,
                ),
                name="beehaiive-agent-output",
                daemon=True,
            )
            for index, (stream, buffer) in enumerate(zip(streams, buffers, strict=True))
            if stream is not None
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out or terminate_descendants:
            CodexExecModelExecutor._terminate_process(process)
        for reader, stream in zip(
            readers, (stream for stream in streams if stream is not None), strict=True
        ):
            reader.join(timeout=POST_TERMINATION_GRACE_SECONDS)
            if reader.is_alive():
                capture_gap("interrupted")
                with suppress(OSError, ValueError):
                    stream.close()
        if handler_errors:
            raise handler_errors[0]
        return (
            buffers[0].decode("utf-8", errors="replace"),
            buffers[1].decode("utf-8", errors="replace"),
            timed_out,
        )


__all__ = ["CodexProcessMixin"]
