from beehaiive.routing import (
    ModelExecution,
    ModelSpec,
)


class FailingRoutingModel:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _spec: ModelSpec, _decision: object) -> ModelExecution:
        self.calls += 1
        error = RuntimeError("provider unavailable")
        error.input_tokens = 3  # type: ignore[attr-defined]
        error.output_tokens = 2  # type: ignore[attr-defined]
        error.failure_context = "The provider rejected the model call."  # type: ignore[attr-defined]
        raise error
