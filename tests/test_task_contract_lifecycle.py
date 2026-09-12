import json
from typing import cast

import pytest
from conftest import FakeProvider
from fastapi.testclient import TestClient

from beehaiive.contracts import (
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
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
    _json_mapping_or_none,
    _task_claimability_for_run,
)
from main import create_app


def snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)),),
    )


class BrokenContract:
    def as_dict(self) -> dict[str, object]:
        raise ContractError("broken contract")


def test_task_contract_is_durable_and_questions_are_answerable(tmp_path) -> None:
    database = tmp_path / "contracts.sqlite3"
    provider = FakeProvider(snapshot())
    store = OrchestratorStore(database)
    service = Orchestrator(store, provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    contract = TaskContract(
        "test.contract", 1, "inspect", {}, (), (), tuple(TaskOutcome)
    )

    assert _json_mapping_or_none("") is None
    assert _json_mapping_or_none("{") is None
    assert _json_mapping_or_none("[]") is None
    assert _json_mapping_or_none('{"ok": true}') == {"ok": True}
    with pytest.raises(StoreError, match="Unknown run"):
        store.ensure_task_contract("missing", contract, lease)
    with pytest.raises(StoreError, match="Unknown run"):
        store.mark_task_question_resumed("missing")
    with pytest.raises(StoreError, match="no answered task question"):
        store.mark_task_question_resumed(run.run_id)
    with store._transaction() as connection:
        assert _task_claimability_for_run(connection, "missing") == (True, False)
    with pytest.raises(StoreError, match="broken contract"):
        store.ensure_task_contract(run.run_id, cast(object, BrokenContract()), lease)  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="active implementation"):
        store.ensure_task_contract(run.run_id, contract, lease)
    with pytest.raises(StoreError, match="Unknown run"):
        store.record_task_result("missing", TaskResult(TaskOutcome.FAIL, {}), lease)
    with pytest.raises(StoreError, match="active implementation"):
        store.record_task_result(run.run_id, TaskResult(TaskOutcome.FAIL, {}), lease)
    with pytest.raises(StoreError, match="Unknown run"):
        service.answer_task_question("missing", "main")

    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease)
    lease = implementation.lease_token or ""
    with pytest.raises(StoreError, match="contract is required"):
        store.record_task_result(run.run_id, TaskResult(TaskOutcome.FAIL, {}), lease)
    with pytest.raises(StoreError, match="contract is required"):
        store.complete_agent_run(run.run_id, "done", lease)
    saved = store.ensure_task_contract(run.run_id, contract, lease)
    assert saved.task_contract == contract.as_dict()
    assert store.ensure_task_contract(run.run_id, contract, lease) == saved
    with pytest.raises(StoreError, match="result is required"):
        store.complete_agent_run(run.run_id, "done", lease)
    with pytest.raises(StoreError, match="pending task question"):
        service.answer_task_question(run.run_id, "main")
    with pytest.raises(StoreError, match="require a question"):
        store.record_task_result(
            run.run_id, TaskResult(TaskOutcome.QUESTION, {}), lease
        )

    store._connection.execute(
        "UPDATE pbis SET claimable = 1 WHERE project_id = ? "
        "AND repository_name = ? AND number = ?",
        ("project-1", "owner/api", 1),
    )
    store._connection.execute(
        "UPDATE runs SET task_result_json = ? WHERE run_id = ?",
        (json.dumps({"outcome": "bogus"}), run.run_id),
    )
    with pytest.raises(StoreError, match="Unknown task outcome"):
        store.complete_agent_run(run.run_id, "done", lease)
    question = TaskResult(
        TaskOutcome.QUESTION, {}, question="Which branch should be used?"
    )
    with pytest.raises(StoreError, match="cannot contain an answer"):
        store.record_task_result(
            run.run_id,
            TaskResult(
                TaskOutcome.QUESTION,
                {},
                question="Which branch?",
                answer="worker-picked-main",
            ),
            lease,
        )
    recorded = store.record_task_result(run.run_id, question, lease)
    assert recorded.task_result == question.as_dict()
    with pytest.raises(StoreError, match="no answered task question"):
        store.mark_task_question_resumed(run.run_id)
    with pytest.raises(StoreError, match="Only a passing"):
        store.complete_agent_run(run.run_id, "done", lease)
    with pytest.raises(StoreError, match="An answer is required"):
        service.answer_task_question(run.run_id, " ")

    store.close()
    reopened = OrchestratorStore(database)
    resumed = Orchestrator(reopened, provider)
    loaded = reopened.get_run(run.run_id)
    assert loaded is not None
    assert loaded.task_contract == contract.as_dict()
    assert loaded.task_result == question.as_dict()
    answered = resumed.answer_task_question(run.run_id, "main")
    assert answered.task_answer == "main"
    assert answered.task_result is not None
    assert resumed.answer_task_question(run.run_id, "main") == answered
    with pytest.raises(StoreError, match="different answer"):
        resumed.answer_task_question(run.run_id, "develop")

    reopened._connection.execute(
        "UPDATE runs SET task_result_json = ? WHERE run_id = ?",
        (
            json.dumps({"outcome": "fail", "evidence": {}, "artifact_refs": []}),
            run.run_id,
        ),
    )
    with pytest.raises(StoreError, match="pending task question"):
        resumed.answer_task_question(run.run_id, "main")
    reopened._connection.execute(
        "UPDATE runs SET task_result_json = ? WHERE run_id = ?",
        (json.dumps({"outcome": "bogus"}), run.run_id),
    )
    with pytest.raises(StoreError, match="Unknown task outcome"):
        resumed.answer_task_question(run.run_id, "main")
    with pytest.raises(StoreError, match="Unknown task outcome"):
        reopened.mark_task_question_resumed(run.run_id)

    passing = TaskResult(TaskOutcome.PASS, {})
    completed = reopened.record_task_result(run.run_id, passing, lease)
    assert completed.task_result == passing.as_dict()
    finished = reopened.complete_agent_run(run.run_id, "done", lease)
    assert finished.status.value == "completed"
    reopened.close()


def test_question_answer_api_returns_the_updated_run(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "question-api.sqlite3")
    service = Orchestrator(store, FakeProvider(snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease)
    contract = TaskContract(
        "test.contract", 1, "inspect", {}, (), (), tuple(TaskOutcome)
    )
    store.ensure_task_contract(run.run_id, contract, implementation.lease_token or "")
    store.record_task_result(
        run.run_id,
        TaskResult(TaskOutcome.QUESTION, {}, question="Which branch?"),
        implementation.lease_token or "",
    )
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )

    response = client.post(
        f"/runs/{run.run_id}/question/answer",
        json={"answer": "main"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["task_answer"] == "main"
    store.close()


def test_sync_does_not_reopen_a_contract_without_a_result(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "missing-result.sqlite3")
    project = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (PbiSnapshot("owner/api", 1, "PBI", stage=Stage.IMPLEMENT),),
            ),
        ),
    )
    store.sync_project(project)
    run = store.claim_next("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    contract = TaskContract(
        "test.contract", 1, "inspect", {}, (), (), tuple(TaskOutcome)
    )
    store.ensure_task_contract(run.run_id, contract, lease)
    store.fail_agent_run(run.run_id, "worker stopped", lease, claimable=True)

    store._connection.execute(
        "UPDATE pbis SET claimable = 1 WHERE project_id = ? "
        "AND repository_name = ? AND number = ?",
        ("project-1", "owner/api", 1),
    )
    store.sync_project(project)

    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    assert store.claim_next("project-1", "owner/api", "worker-2") is None
    store.close()


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
