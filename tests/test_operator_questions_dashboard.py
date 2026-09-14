from fastapi.testclient import TestClient

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.conftest import FakeProvider
from tests.support.task_contract.helpers import snapshot


def test_dashboard_can_answer_the_current_operator_question_without_logging_answer(
    tmp_path,
) -> None:
    store = OrchestratorStore(tmp_path / "operator-question-dashboard.sqlite3")
    orchestrator = Orchestrator(store, FakeProvider(snapshot()))
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = (
        store.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "").lease_token
        or ""
    )
    contract = TaskContract.inventory(run.repository, run.pbi_number, run.title)
    store.ensure_task_contract(run.run_id, contract, lease)
    result = TaskResult(TaskOutcome.QUESTION, {}, question="Which branch?")
    store.record_task_result(run.run_id, result, lease)
    question = store.await_operator(
        run.run_id,
        lease,
        kind="question",
        question=result.question or "",
        evidence=result.evidence,
    )
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            api_key="test-api-key",
            allowed_project_ids={"project-1"},
        )
    )

    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-api-key"},
        json={
            "action": "answer_question",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run.run_id,
            "question_id": question["question_id"],
            "revision": question["revision"],
            "answer": "main",
        },
    )

    assert response.status_code == 200
    result_data = response.json()["result"]
    assert result_data["question_status"] == "answered"
    assert result_data["run_status"] == "awaiting_operator"
    state_pbi = response.json()["state"]["repositories"][0]["pbis"][0]
    assert state_pbi["operator_questions"][0]["answer"] == "main"
    action = store.actions_for_project("project-1")[0]
    assert action["request"]["answer"] == "[redacted]"
    assert "main" not in str(action["result"])
    store.close()
