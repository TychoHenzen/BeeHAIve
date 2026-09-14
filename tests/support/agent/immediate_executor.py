from pathlib import Path

from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.routing import (
    ModelExecution,
)


class ImmediateExecutor(CodexExecModelExecutor):
    def __init__(self, result: ModelExecution, repository: Path | None = None) -> None:
        super().__init__(repository or Path.cwd(), repository_name="owner/api")
        self.result = result

    def execute(self, spec, decision) -> ModelExecution:
        del spec, decision
        return self.result
