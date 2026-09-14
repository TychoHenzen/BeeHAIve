from threading import Event

from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelSpec,
)


class BlockingRoutingModel:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.models: list[str] = []

    def execute(self, spec: ModelSpec, _decision: object) -> ModelExecution:
        self.models.append(spec.model)
        if len(self.models) == 1:
            self.started.set()
            self.release.wait(timeout=2)
        return ModelExecution(
            AttemptOutcome.FAILURE,
            input_tokens=1,
            output_tokens=1,
            failure_context="The concurrent model attempt failed.",
        )
