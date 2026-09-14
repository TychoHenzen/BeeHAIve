from pathlib import Path

from beehaiive.agent import CodexExecModelExecutor
from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import (
    RunState,
)
from beehaiive.routing import AttemptOutcome, ModelExecution


class ImmediateDemoExecutor(CodexExecModelExecutor):
    def __init__(self, repository: Path | None = None) -> None:
        super().__init__(repository or Path.cwd(), repository_name="owner/api")

    def build_task_contract(self, run: RunState) -> TaskContract:
        return TaskContract.inventory(run.repository, run.pbi_number, run.title)

    def execute(self, spec, decision) -> ModelExecution:
        del spec, decision
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            result="demo result",
            task_result=TaskResult(TaskOutcome.PASS, {}),
        )
