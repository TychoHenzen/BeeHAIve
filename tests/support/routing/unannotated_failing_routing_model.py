from beehaiive.routing import (
    ModelExecution,
    ModelSpec,
)


class UnannotatedFailingRoutingModel:
    def execute(self, _spec: ModelSpec, _decision: object) -> ModelExecution:
        raise RuntimeError("provider unavailable")
