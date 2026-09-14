from __future__ import annotations

import pytest

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
    _task_claimability_for_run,
)
from tests.conftest import FakeProvider
from tests.support.task_contract.helpers import snapshot


def test_operator_question_is_durable_and_blocks_claim_until_answer(
    tmp_path,
) -> None:
    database = tmp_path / "operator-question.sqlite3"
    provider = FakeProvider(snapshot())
    store = OrchestratorStore(database)
    orchestrator = Orchestrator(store, provider)
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease)
    lease = implementation.lease_token or ""
    contract = TaskContract.inventory(
        implementation.repository, implementation.pbi_number, implementation.title
    )
    store.ensure_task_contract(run.run_id, contract, lease)
    task_result = TaskResult(
        TaskOutcome.QUESTION, {}, question="Which branch should be used?"
    )
    store.record_task_result(run.run_id, task_result, lease)

    question = store.await_operator(
        run.run_id,
        lease,
        kind="question",
        question=task_result.question or "",
        evidence=task_result.evidence,
    )
    duplicate = store.await_operator(
        run.run_id,
        lease,
        kind="question",
        question=task_result.question or "",
        evidence=task_result.evidence,
    )

    assert duplicate == question
    assert question["run_id"] == run.run_id
    assert question["evidence"]["worker_id"] == "worker-1"
    assert question["revision"] == 1
    assert question["status"] == "pending"
    assert question["notification_status"] == "pending"
    waiting = store.get_run(run.run_id)
    assert waiting is not None
    assert waiting.status.value == "awaiting_operator"
    assert waiting.lease_token is None
    assert (
        store.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    assert orchestrator.claim("project-1", "owner/api", "worker-2") is None

    store.close()
    reopened = OrchestratorStore(database)
    recovered = reopened.get_run(run.run_id)
    persisted = reopened.operator_question_for_run(run.run_id)
    assert recovered is not None
    assert recovered.status.value == "awaiting_operator"
    assert persisted == question
    assert (
        Orchestrator(reopened, provider).claim("project-1", "owner/api", "worker-2")
        is None
    )

    with pytest.raises(StoreError, match="stale"):
        reopened.answer_operator_question(
            run.run_id,
            question_id=question["question_id"],
            revision=2,
            answer="main",
            authorization_method="X-API-Key",
        )
    answered = reopened.answer_operator_question(
        run.run_id,
        question_id=question["question_id"],
        revision=question["revision"],
        answer="main",
        authorization_method="X-API-Key",
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == "main"
    assert (
        reopened.answer_operator_question(
            run.run_id,
            question_id=question["question_id"],
            revision=question["revision"],
            answer="main",
            authorization_method="X-API-Key",
        )
        == answered
    )
    with pytest.raises(StoreError, match="different answer"):
        reopened.answer_operator_question(
            run.run_id,
            question_id=question["question_id"],
            revision=question["revision"],
            answer="develop",
            authorization_method="X-API-Key",
        )
    reopened.mark_task_question_resumed(run.run_id)
    resumed_run = reopened.get_run(run.run_id)
    assert resumed_run is not None
    assert resumed_run.status.value == "awaiting_operator"
    assert (
        reopened.project_state("project-1")["repositories"][0]["pbis"][0]["claimable"]
        is True
    )
    with reopened._transaction() as connection:
        assert _task_claimability_for_run(connection, run.run_id) == (False, True)
    claimed = Orchestrator(reopened, provider).claim(
        "project-1", "owner/api", "worker-2"
    )
    assert claimed is not None
    assert claimed.run_id == run.run_id
    assert claimed.attempt == 2
    assert claimed.status.value == "active"
    reopened.close()


def test_routing_exhaustion_answer_is_added_to_the_next_task_contract(
    tmp_path,
) -> None:
    provider = FakeProvider(snapshot())
    store = OrchestratorStore(tmp_path / "routing-exhausted-question.sqlite3")

    class ContractBuilder:
        def build_task_contract(self, run):
            return TaskContract.inventory(run.repository, run.pbi_number, run.title)

    orchestrator = Orchestrator(store, provider, model_executor=ContractBuilder())
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = (
        store.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "").lease_token
        or ""
    )
    contract = TaskContract.inventory(run.repository, run.pbi_number, run.title)
    store.ensure_task_contract(run.run_id, contract, lease)
    blocked = TaskResult(
        TaskOutcome.BLOCKED, {}, required_action="Choose a branch before continuing"
    )
    store.record_task_result(run.run_id, blocked, lease)
    question = store.await_operator(
        run.run_id,
        lease,
        kind="routing_exhausted",
        question=blocked.required_action or "",
        evidence=blocked.evidence,
    )

    answered = store.answer_operator_question(
        run.run_id,
        question_id=question["question_id"],
        revision=question["revision"],
        answer="Use the main branch",
        authorization_method="X-API-Key",
    )
    assert answered["status"] == "answered"
    store.mark_task_question_resumed(run.run_id)
    resumed = store.get_run(run.run_id)
    assert resumed is not None
    with store._transaction() as connection:
        assert _task_claimability_for_run(connection, run.run_id) == (False, True)

    contract_with_answer = orchestrator._task_contract_for_run(resumed)

    assert contract_with_answer.inputs["answer"] == "Use the main branch"
    store.close()


def test_project_sync_preserves_question_block_and_answered_claimability(
    tmp_path,
) -> None:
    store = OrchestratorStore(tmp_path / "question-sync.sqlite3")
    orchestrator = Orchestrator(store, FakeProvider(snapshot()))
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    failed = store.fail(run.run_id, "Routing exhausted", run.lease_token or "")
    assert failed.status.value == "failed"
    question = store.await_operator_after_failure(
        run.run_id,
        kind="routing_exhausted",
        question="Choose a routing target",
        evidence={},
    )

    store.sync_project(snapshot())
    waiting_pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert waiting_pbi["claimable"] is False

    store.answer_operator_question(
        run.run_id,
        question_id=str(question["question_id"]),
        revision=int(question["revision"]),
        answer="Use main",
        authorization_method="X-API-Key",
    )
    store.mark_task_question_resumed(run.run_id)
    store.sync_project(snapshot())
    resumed_pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert resumed_pbi["claimable"] is True
    store.close()


def test_new_operator_question_revision_clears_prior_resumed_claimability(
    tmp_path,
) -> None:
    store = OrchestratorStore(tmp_path / "question-revision-sync.sqlite3")
    orchestrator = Orchestrator(store, FakeProvider(snapshot()))
    orchestrator.synchronize("project-1")
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, run.lease_token or "")
    first_question = store.await_operator(
        run.run_id,
        implementation.lease_token or "",
        kind="question",
        question="Which branch should be used?",
        evidence={},
    )
    store.answer_operator_question(
        run.run_id,
        question_id=str(first_question["question_id"]),
        revision=int(first_question["revision"]),
        answer="Use main",
        authorization_method="X-API-Key",
    )
    store.mark_task_question_resumed(run.run_id)
    resumed = orchestrator.claim("project-1", "owner/api", "worker-2")
    assert resumed is not None
    failed = store.fail(resumed.run_id, "Routing exhausted", resumed.lease_token or "")
    assert failed.status.value == "failed"
    second_question = store.await_operator_after_failure(
        resumed.run_id,
        kind="routing_exhausted",
        question="Choose a routing target",
        evidence={},
    )

    store.sync_project(snapshot())
    waiting_pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert second_question["revision"] == 2
    assert waiting_pbi["claimable"] is False
    store.close()
