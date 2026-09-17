from beehaiive.agent import (
    AgentWorkerManager,
    format_worker_exception,
)
from beehaiive.models import (
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_worker_exception_detail_includes_missing_path() -> None:
    try:
        raise FileNotFoundError(
            2, "The system cannot find the file specified", "missing.exe"
        )
    except FileNotFoundError as error:
        detail = format_worker_exception(error)

    assert "FileNotFoundError" in detail
    assert "filename='missing.exe'" in detail
    assert "Missing path details" in detail


def test_worker_manager_recovers_when_failure_lease_is_lost() -> None:
    run = RunState(
        "lease-loss-run",
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

    class LeaseLossStore:
        def __init__(self) -> None:
            self.recovery: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            del run_id, error, lease_token
            raise StoreError("lease changed")

        def fail_agent_run_after_lease_loss(
            self, run_id: str, error: str, *, expected_lease_token: str
        ) -> None:
            self.recovery = (run_id, error, expected_lease_token)

    class LeaseLossOrchestrator:
        def __init__(self) -> None:
            self.store = LeaseLossStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token
            raise RuntimeError("worker exploded")

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = LeaseLossOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.recovery is not None
    assert orchestrator.store.recovery[0] == run.run_id
    assert "Agent worker failed:" in orchestrator.store.recovery[1]
    assert "RuntimeError: worker exploded" in orchestrator.store.recovery[1]
    assert "Traceback" in orchestrator.store.recovery[1]
    assert orchestrator.store.recovery[2] == "lease-1"
