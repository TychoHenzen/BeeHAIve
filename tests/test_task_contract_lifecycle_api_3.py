import json

from beehaiive.contracts import (
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
)
from tests.conftest import FakeProvider
from tests.support.task_contract.helpers import snapshot as snapshot


def test_lease_loss_after_answer_keeps_question_claimable(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "question-lease-loss.sqlite3")
    service = Orchestrator(store, FakeProvider(snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    implementation = service.advance(run.run_id, Stage.IMPLEMENT, lease)
    contract = TaskContract(
        "test.contract", 1, "inspect", {}, (), (), tuple(TaskOutcome)
    )
    store.ensure_task_contract(run.run_id, contract, implementation.lease_token or "")
    store.record_task_result(
        run.run_id,
        TaskResult(TaskOutcome.QUESTION, {}, question="Which branch?"),
        implementation.lease_token or "",
    )

    answered = service.answer_task_question(run.run_id, "main")
    store.mark_task_question_resumed(run.run_id)
    failed = store.fail_agent_run_after_lease_loss(
        run.run_id,
        "stale worker handoff",
        expected_lease_token=implementation.lease_token or "",
    )

    assert failed.status.value == "failed"
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is True
    )
    event = store._connection.execute(
        "SELECT details_json FROM events WHERE run_id = ? "
        "AND event_type = 'failure' ORDER BY event_id DESC LIMIT 1",
        (run.run_id,),
    ).fetchone()
    assert event is not None
    assert json.loads(event["details_json"])["superseded_by_answer"] is True
    resumed = service.claim("project-1", "owner/api", "worker-2")
    assert resumed is not None
    assert resumed.task_answer == answered.task_answer
    store.close()
