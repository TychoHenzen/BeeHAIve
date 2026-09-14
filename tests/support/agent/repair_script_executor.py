import sys
from pathlib import Path

from beehaiive.agent import (
    CodexExecModelExecutor,
)


class RepairScriptExecutor(CodexExecModelExecutor):
    def __init__(
        self, repository: Path, script: Path, timeout_seconds: float = 5
    ) -> None:
        super().__init__(
            repository,
            executable=sys.executable,
            timeout_seconds=timeout_seconds,
            repository_name="owner/api",
        )
        self.script = script

    def _repair_command(
        self, prompt: str, repository: Path, model: str | None = None
    ) -> list[str]:
        del model
        return [sys.executable, str(self.script), "--cd", str(repository), prompt]
