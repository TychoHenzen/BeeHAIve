import json
from typing import cast

import pytest
from fastapi.testclient import TestClient

from beehaiive.contracts import (
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
    _json_mapping_or_none,
    _task_claimability_for_run,
)
from main import create_app
from tests.conftest import FakeProvider
from tests.support.task_contract.broken_contract import BrokenContract as BrokenContract
from tests.support.task_contract.helpers import snapshot as snapshot


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
    question_result = TaskResult(TaskOutcome.QUESTION, {}, question="Which branch?")
    store.record_task_result(
        run.run_id,
        question_result,
        implementation.lease_token or "",
    )
    question = store.await_operator(
        run.run_id,
        implementation.lease_token or "",
        kind="question",
        question=question_result.question or "",
        evidence=question_result.evidence,
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
        json={
            "question_id": question["question_id"],
            "revision": question["revision"],
            "answer": "main",
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["task_answer"] == "main"
    assert response.json()["operator_question"]["status"] == "answered"
    store.close()
