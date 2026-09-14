import json

import pytest

from beehaiive.contracts import (
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    RoutingError,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from tests.conftest import FakeProvider
from tests.support.task_contract.helpers import snapshot as snapshot


def test_question_answer_reopens_routing_and_redacts_operator_text(
    tmp_path, monkeypatch
) -> None:
    store = OrchestratorStore(tmp_path / "question-routing.sqlite3")
    routing_database = tmp_path / "question-routing-route.sqlite3"
    routing_store = RoutingStore(routing_database)
    router = ModelRouter(routing_store)
    service = Orchestrator(store, FakeProvider(snapshot()), router)
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
    router.record(
        run.run_id,
        AttemptOutcome.FAILURE,
        failure_context="Which branch?",
        force_human_reason="Which branch?",
    )
    service.synchronize("project-1")
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    assert service.claim("project-1", "owner/api", "worker-2") is None
    with pytest.raises(RoutingError, match="recovery reason"):
        router.reopen_human_handoff(run.run_id, " ")

    supplied_answer = " token=answer-secret " + ("x" * 1_100)
    reopen = router.reopen_human_handoff

    def reject_reopen(*args, **kwargs):
        del args, kwargs
        raise RoutingError("route store unavailable")

    monkeypatch.setattr(router, "reopen_human_handoff", reject_reopen)
    with pytest.raises(StoreError, match="route store unavailable"):
        service.answer_task_question(run.run_id, supplied_answer)

    pending = store.get_run(run.run_id)
    assert pending is not None
    assert pending.task_answer is not None
    assert "answer-secret" not in pending.task_answer
    assert len(pending.task_answer) <= 1_000
    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert pbi["claimable"] is False
    event = store._connection.execute(
        "SELECT details_json FROM events WHERE run_id = ? "
        "AND event_type = 'question_answered'",
        (run.run_id,),
    ).fetchone()
    assert event is not None
    assert "answer-secret" not in event["details_json"]

    monkeypatch.setattr(router, "reopen_human_handoff", reopen)
    answer = service.answer_task_question(run.run_id, supplied_answer)

    assert answer.task_answer is not None
    assert "answer-secret" not in answer.task_answer
    assert len(answer.task_answer) <= 1_000
    assert router.snapshot(run.run_id).state.status is RoutingStatus.ACTIVE
    failed = store.fail_agent_run(
        run.run_id, "stale worker handoff", lease, claimable=False
    )
    assert failed.status.value == "failed"
    service.synchronize("project-1")
    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert pbi["claimable"] is True
    resumed_run = service.claim("project-1", "owner/api", "worker-2")
    assert resumed_run is not None
    assert resumed_run.task_answer == answer.task_answer

    recovered_router = ModelRouter(RoutingStore(routing_database))
    assert recovered_router.snapshot(run.run_id).state.status is RoutingStatus.ACTIVE

    class PassingModel:
        def execute(self, _spec, _decision) -> ModelExecution:
            return ModelExecution(AttemptOutcome.SUCCESS)

    resumed = recovered_router.execute(run.run_id, PassingModel())
    assert resumed.state.status is RoutingStatus.RESOLVED
    with pytest.raises(StoreError, match="not resumable"):
        service.answer_task_question(run.run_id, supplied_answer)
    service.synchronize("project-1")
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    store.close()
    routing_store.close()
    recovered_router.store.close()


def test_question_answer_stays_nonclaimable_when_routing_is_not_resumable(
    tmp_path,
) -> None:
    store = OrchestratorStore(tmp_path / "question-not-resumable.sqlite3")
    routing_store = RoutingStore(tmp_path / "question-not-resumable-route.sqlite3")
    router = ModelRouter(routing_store)
    service = Orchestrator(store, FakeProvider(snapshot()), router)
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
    router.record(run.run_id, AttemptOutcome.SUCCESS)
    store.fail_agent_run(run.run_id, "Which branch?", lease, claimable=False)

    with pytest.raises(StoreError, match="not resumable"):
        service.answer_task_question(run.run_id, "main")

    saved = store.get_run(run.run_id)
    assert saved is not None
    assert saved.task_answer == "main"
    service.synchronize("project-1")
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    store._connection.execute(
        "UPDATE runs SET task_contract_json = ? WHERE run_id = ?",
        ("", run.run_id),
    )
    service.synchronize("project-1")
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    store._connection.execute(
        "UPDATE runs SET task_contract_json = ?, task_result_json = ? WHERE run_id = ?",
        (json.dumps(contract.as_dict()), "not-json", run.run_id),
    )
    service.synchronize("project-1")
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    store.close()
    routing_store.close()
