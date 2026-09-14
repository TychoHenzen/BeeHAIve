from collections.abc import Callable

from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelSpec,
)
from beehaiive.workflow import (
    WorkspaceLease,
)

from .fake_routing_model import FakeRoutingModel as FakeRoutingModel


class LeasedFakeRoutingModel(FakeRoutingModel):
    def __init__(self) -> None:
        super().__init__(
            (AttemptOutcome.SUCCESS,),
            input_tokens=7,
            output_tokens=3,
            task_result=TaskResult(TaskOutcome.PASS, {}),
        )
        self.workspace_path: str | None = None
        self.workspace_validator: Callable[[], None] | None = None
        self.release_calls = 0

    def prepare_run(
        self,
        _problem_id: str,
        _repository: str,
        workspace_lease: WorkspaceLease | None = None,
        validate_workspace_lease: Callable[[], None] | None = None,
    ) -> None:
        assert workspace_lease is not None
        assert validate_workspace_lease is not None
        self.workspace_path = workspace_lease.worktree_path
        self.workspace_validator = validate_workspace_lease
        validate_workspace_lease()

    def release_run(self, _problem_id: str) -> None:
        self.release_calls += 1
        self.workspace_path = None
        self.workspace_validator = None

    def execute(self, spec: ModelSpec, decision: object) -> ModelExecution:
        assert self.workspace_path is not None
        assert self.workspace_validator is not None
        self.workspace_validator()
        return super().execute(spec, decision)
