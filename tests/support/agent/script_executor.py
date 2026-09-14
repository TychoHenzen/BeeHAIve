import sys
from pathlib import Path

from beehaiive.agent import (
    CodexExecModelExecutor,
)


class ScriptExecutor(CodexExecModelExecutor):
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

    def _command(self, prompt: str) -> list[str]:
        return [
            sys.executable,
            str(self.script),
            "--cd",
            str(self.repository),
            prompt,
        ]
