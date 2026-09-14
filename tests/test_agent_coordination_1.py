from types import SimpleNamespace

from beehaiive.agent import (
    AgentWorkerManager,
)
from beehaiive.models import (
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    RoutingStatus,
)
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_manager_persists_human_handoff_without_claimability() -> None:
    run = RunState(
        "handoff-run",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class HandoffStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str, bool | None] | None = None

        def fail_agent_run(
            self,
            run_id: str,
            error: str,
            lease_token: str,
            *,
            claimable: bool | None = None,
        ) -> None:
            self.failure = (run_id, error, lease_token, claimable)

    class HandoffOrchestrator:
        def __init__(self) -> None:
            self.store = HandoffStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token

        def run_implementation_attempt(self, run_id: str, lease_token: str):
            del run_id, lease_token
            return SimpleNamespace(
                state=SimpleNamespace(
                    status=RoutingStatus.HUMAN_HANDOFF,
                    required_action="Human approval is required",
                ),
                attempt=None,
                decision=SimpleNamespace(failure_context=""),
                execution_result=None,
            )

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = HandoffOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.failure == (
        run.run_id,
        "Human approval is required",
        "lease-1",
        False,
    )
