from beehaiive.contracts import TaskResult
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelSpec,
)


class FakeRoutingModel:
    def __init__(
        self,
        outcomes: tuple[AttemptOutcome, ...] = (AttemptOutcome.FAILURE,),
        *,
        input_tokens: int = 1,
        output_tokens: int = 1,
        recursive_spawn_depth: int = 0,
        task_result: TaskResult | None = None,
    ) -> None:
        self.calls = 0
        self.outcomes = outcomes
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.recursive_spawn_depth = recursive_spawn_depth
        self.task_result = task_result
        self.models: list[str] = []

    def run(self) -> AttemptOutcome:
        self.calls += 1
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]

    def execute(self, spec: ModelSpec, _decision: object) -> ModelExecution:
        self.models.append(spec.model)
        outcome = self.run()
        return ModelExecution(
            outcome,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            failure_context="The fake model did not resolve the problem.",
            recursive_spawn_depth=self.recursive_spawn_depth,
            task_result=self.task_result,
        )
