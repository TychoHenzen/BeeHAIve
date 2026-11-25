"""Run the Archon command-line tool for the features its HTTP server lacks.

Archon's server exposes runs, attention and projects, but triggers, schedules,
state and isolation exist only in the CLI. Every call here passes a fixed
argument list, never a shell string, and the routes that use it accept only
validated names, so a browser cannot choose which command runs.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CliResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        return "\n".join(
            part for part in (self.stdout.strip(), self.stderr.strip()) if part
        )


Runner = Callable[[Sequence[str], Path | None, float], CliResult]


class ArchonCliError(RuntimeError):
    """A command that failed. `message` is written for the person, and `detail`
    is what the command printed. Neither holds an exception's own text."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def run_process(args: Sequence[str], cwd: Path | None, timeout: float) -> CliResult:
    try:
        done = subprocess.run(
            list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            # No console window flashes up on Windows; 0 elsewhere.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except FileNotFoundError as error:
        raise ArchonCliError(f"{args[0]} was not found.") from error
    except subprocess.TimeoutExpired as error:
        raise ArchonCliError(
            f"{Path(args[0]).name} did not finish in {timeout:.0f}s."
        ) from error
    return CliResult(done.returncode, done.stdout or "", done.stderr or "")


class ArchonCli:
    def __init__(
        self, binary: Path, runner: Runner = run_process, timeout: float = 60.0
    ) -> None:
        self.binary = binary
        self._runner = runner
        self._timeout = timeout

    def run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float | None = None
    ) -> CliResult:
        return self._runner([str(self.binary), *args], cwd, timeout or self._timeout)

    def json(self, args: Sequence[str], cwd: Path | None = None) -> object:
        """Run a command that prints JSON, and parse it.

        Archon prints notices such as "stripped 5 keys from .env" on stderr, so
        stdout holds only the document.
        """
        result = self.run(args, cwd)
        if not result.ok:
            raise ArchonCliError(_first_line(result) or "archon failed.", result.output)
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise ArchonCliError(
                f"archon {' '.join(args[:2])} did not print JSON.", result.output
            ) from error


def _first_line(result: CliResult) -> str:
    for line in (result.stderr + "\n" + result.stdout).splitlines():
        text = line.strip()
        if text and not text.startswith("[archon] stripped"):
            return text
    return ""
