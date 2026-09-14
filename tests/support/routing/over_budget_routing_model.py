from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelSpec,
    RoutingDecision,
)


class OverBudgetRoutingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.budgets: list[tuple[int, int, int]] = []

    def execute(self, _spec: ModelSpec, decision: RoutingDecision) -> ModelExecution:
        self.calls += 1
        self.budgets.append(
            (
                decision.remaining_tokens,
                decision.remaining_rounds,
                decision.remaining_recursive_spawn_depth,
            )
        )
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            input_tokens=self.budgets[-1][0] + 1,
        )
