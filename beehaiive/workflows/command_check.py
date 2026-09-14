from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .check_result import CheckResult
from .workflow_error import WorkflowError


@dataclass(frozen=True, slots=True)
class CommandCheck:
    """Run one fixed command without a shell and capture its evidence."""

    name: str
    command: tuple[str, ...]
    timeout_seconds: float = 60.0

    def run(self, workspace: Path) -> CheckResult:
        if not self.command:
            raise WorkflowError(f"Command check {self.name} has no command")
        result = subprocess.run(
            self.command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        evidence = output[-4_000:] if output else f"exit code {result.returncode}"
        return CheckResult(self.name, result.returncode == 0, evidence)
