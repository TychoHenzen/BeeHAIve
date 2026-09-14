from __future__ import annotations

import pytest
from conftest import FakeProvider
from fastapi.testclient import TestClient

from beehaiive.models import ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from main import create_app

PROJECT_ID = "project-1"
REPOSITORY = "owner/api"


def _seed_store(store: OrchestratorStore) -> None:
    store.sync_project(
        ProjectSnapshot(PROJECT_ID, "Planning", (RepositorySnapshot(REPOSITORY),))
    )


def _questions(*texts: str) -> list[dict[str, object]]:
    return [{"text": text, "evidence_refs": []} for text in texts]


def test_refinement_attempt_persists_answer_history_and_reopen_results(
    tmp_path,
) -> None:
    database = tmp_path / "refinement.sqlite3"
    store = OrchestratorStore(database)
    _seed_store(store)
    attempt = store.create_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        _questions("Which behavior is required?"),
        operator_role="operator",
    )
    question_id = attempt.questions[0].question_id

    answered = store.answer_pbi_refinement_question(
        PROJECT_ID,
        REPOSITORY,
        64,
        question_id,
        "Preserve the current behavior.",
        expected_revision=0,
        evidence_refs=("https://github.com/owner/api/issues/64",),
        operator_role="operator",
    )
    assert answered.status == "evaluating"
    assert answered.attempt_id == attempt.attempt_id
    assert answered.revision == 1

    replay = store.answer_pbi_refinement_question(
        PROJECT_ID,
        REPOSITORY,
        64,
        question_id,
        "Preserve the current behavior.",
        expected_revision=0,
        evidence_refs=("https://github.com/owner/api/issues/64",),
        operator_role="operator",
    )
    assert replay.revision == 1
    assert len(replay.questions[0].answer_history) == 1

    with pytest.raises(StoreError, match="current revision"):
        store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question_id,
            "Use the refined behavior.",
            expected_revision=0,
            operator_role="operator",
        )
    corrected = store.answer_pbi_refinement_question(
        PROJECT_ID,
        REPOSITORY,
        64,
        question_id,
        "Use the refined behavior.",
        expected_revision=1,
        operator_role="operator",
    )
    assert len(corrected.questions[0].answer_history) == 2

    completed = store.complete_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        expected_revision=2,
        summary="The behavior is defined.",
        operator_role="operator",
    )
    store.close()

    reopened_store = OrchestratorStore(database)
    restored = reopened_store.get_pbi_refinement_attempt(
        PROJECT_ID, REPOSITORY, 64, operator_role="operator"
    )
    assert restored is not None
    assert restored.attempt_id == attempt.attempt_id
    assert restored.status == "completed"
    assert restored.questions[0].answer_history == corrected.questions[0].answer_history
    assert restored.decision == completed.decision
    assert restored.authorization["role"] == "operator"
    assert restored.authorization["method"] == "X-API-Key"
    assert restored.authorization["result"] == "authorized"
    assert restored.authorization["timestamp"]

    next_generation = reopened_store.reopen_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        reason="The owner requested one more detail.",
        questions=_questions("What evidence supports the behavior?"),
        operator_role="operator",
    )
    assert next_generation.attempt_id == attempt.attempt_id
    assert next_generation.generation == 2
    assert next_generation.reopen_count == 1
    assert next_generation.status == "awaiting_answers"
    assert next_generation.history[0]["decision"] == completed.decision
    assert next_generation.history[0]["reopen_reason"] == (
        "The owner requested one more detail."
    )
    reopened_store.close()


def test_refinement_limits_redaction_and_reopen_cap() -> None:
    store = OrchestratorStore()
    _seed_store(store)
    with pytest.raises(StoreError, match="25 questions"):
        store.create_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            64,
            _questions(*(f"Question {index}" for index in range(26))),
            operator_role="operator",
        )
    for parameter in ("X-Amz-Signature", "refresh_token", "client_secret"):
        with pytest.raises(StoreError, match="(?i)signed URL"):
            store.create_pbi_refinement_attempt(
                PROJECT_ID,
                REPOSITORY,
                64,
                _questions(f"See https://example.test/file?{parameter}=secret"),
                operator_role="operator",
            )

    credential_text = store.create_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        66,
        _questions("refresh_token=refresh-secret client_secret=client-secret"),
        operator_role="operator",
    )
    redacted_question = credential_text.questions[0].text
    assert redacted_question == ("refresh_token=[redacted] client_secret=[redacted]")
    persisted = store._connection.execute(
        "SELECT questions_json FROM pbi_refinement_attempts WHERE pbi_number = 66"
    ).fetchone()
    assert persisted is not None
    assert "refresh-secret" not in persisted["questions_json"]
    assert "client-secret" not in persisted["questions_json"]

    attempt = store.create_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        _questions("Which behavior is required?"),
        operator_role="operator",
        secret_values=("private-api-key",),
    )
    question_id = attempt.questions[0].question_id
    with pytest.raises(StoreError, match="1,000 characters"):
        store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question_id,
            "x" * 1_001,
            expected_revision=0,
            operator_role="operator",
        )
    with pytest.raises(StoreError, match="at most 10 evidence references"):
        store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question_id,
            "Answer",
            expected_revision=0,
            evidence_refs=tuple(f"ref-{index}" for index in range(11)),
            operator_role="operator",
        )

    current = store.answer_pbi_refinement_question(
        PROJECT_ID,
        REPOSITORY,
        64,
        question_id,
        "Initial answer",
        expected_revision=0,
        operator_role="operator",
    )
    for revision in range(1, 4):
        current = store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question_id,
            f"Correction {revision}",
            expected_revision=revision,
            operator_role="operator",
        )
    with pytest.raises(StoreError, match="three corrected answers"):
        store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question_id,
            "Correction 4",
            expected_revision=4,
            operator_role="operator",
        )
    assert len(current.questions[0].answer_history) == 4

    current = store.fail_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        expected_revision=current.revision,
        reason="The evaluator needs another input.",
        retryable=True,
        operator_role="operator",
    )
    for generation in (2, 3):
        current = store.reopen_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            64,
            reason=f"Retry refinement generation {generation}.",
            questions=_questions(f"Generation {generation} question?"),
            operator_role="operator",
        )
        current = store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            current.questions[0].question_id,
            f"Answer for generation {generation}",
            expected_revision=0,
            operator_role="operator",
        )
        current = store.fail_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            64,
            expected_revision=current.revision,
            reason="Retryable evaluation failure.",
            retryable=True,
            operator_role="operator",
        )
    with pytest.raises(StoreError, match="two reopens"):
        store.reopen_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            64,
            reason="No automatic third reopen.",
            questions=_questions("One more question?"),
            operator_role="operator",
        )
    store.close()


def test_refinement_accepts_caps_and_rejects_overflow_without_truncation() -> None:
    store = OrchestratorStore()
    _seed_store(store)
    evidence_refs = [f"{index:02d}-" + "e" * 509 for index in range(10)]
    bounded_questions = [
        {"text": "q" * 1_000, "evidence_refs": evidence_refs} for _ in range(25)
    ]
    attempt = store.create_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        bounded_questions,
        operator_role="operator",
    )
    assert len(attempt.questions) == 25
    assert len(attempt.questions[0].text) == 1_000
    assert len(attempt.questions[0].evidence_refs) == 10
    assert all(
        len(reference) == 512 for reference in attempt.questions[0].evidence_refs
    )

    with pytest.raises(StoreError, match="1,000 characters"):
        store.create_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            65,
            _questions("q" * 1_001),
            operator_role="operator",
        )
    with pytest.raises(StoreError, match="at most 10 evidence references"):
        store.create_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            65,
            [{"text": "Question?", "evidence_refs": [*evidence_refs, "extra"]}],
            operator_role="operator",
        )
    unchanged = store.get_pbi_refinement_attempt(
        PROJECT_ID, REPOSITORY, 64, operator_role="operator"
    )
    assert unchanged is not None
    assert unchanged.revision == 0
    assert len(unchanged.questions[0].evidence_refs) == 10
    with pytest.raises(StoreError, match="512 characters"):
        store.create_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            65,
            [{"text": "Question?", "evidence_refs": ["r" * 513]}],
            operator_role="operator",
        )

    current = attempt
    for question in attempt.questions:
        with pytest.raises(StoreError, match="at most 10 evidence references"):
            store.answer_pbi_refinement_question(
                PROJECT_ID,
                REPOSITORY,
                64,
                question.question_id,
                "a" * 1_000,
                expected_revision=0,
                evidence_refs=("additional-ref",),
                operator_role="operator",
            )
        current = store.answer_pbi_refinement_question(
            PROJECT_ID,
            REPOSITORY,
            64,
            question.question_id,
            "a" * 1_000,
            expected_revision=0,
            operator_role="operator",
        )
    assert current.revision == 25
    assert current.status == "evaluating"
    completed = store.complete_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        expected_revision=25,
        summary="d" * 1_000,
        evidence_refs=evidence_refs,
        operator_role="operator",
    )
    assert len(completed.decision["summary"]) == 1_000

    reopened = store.reopen_pbi_refinement_attempt(
        PROJECT_ID,
        REPOSITORY,
        64,
        reason="r" * 500,
        questions=bounded_questions,
        operator_role="operator",
    )
    assert len(reopened.history[0]["reopen_reason"]) == 500
    with pytest.raises(StoreError, match="500 characters"):
        store.reopen_pbi_refinement_attempt(
            PROJECT_ID,
            REPOSITORY,
            64,
            reason="r" * 501,
            questions=bounded_questions,
            operator_role="operator",
        )
    assert reopened.generation == 2
    store.close()


def test_refinement_api_requires_project_repository_key_and_operator() -> None:
    store = OrchestratorStore()
    _seed_store(store)
    provider = FakeProvider(
        ProjectSnapshot(PROJECT_ID, "Planning", (RepositorySnapshot(REPOSITORY),))
    )
    service = Orchestrator(store, provider)
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={PROJECT_ID},
            workflow_actor="operator",
        )
    )
    path = f"/projects/{PROJECT_ID}/repositories/{REPOSITORY}/pbis/64/refinement"
    payload = {"questions": [{"text": "Which behavior is required?"}]}

    assert client.post(path, json=payload).status_code == 401
    first = client.post(path, headers={"X-API-Key": "test-key"}, json=payload)
    assert first.status_code == 200
    created = client.post(path, headers={"X-API-Key": "test-key"}, json=payload).json()
    assert created["attempt_id"] == first.json()["attempt_id"]
    attempt_id = created["attempt_id"]
    question_id = created["questions"][0]["question_id"]
    answered = client.post(
        f"{path}/questions/{question_id}/answer",
        headers={"X-API-Key": "test-key"},
        json={"answer": "The answer mentions test-key.", "expected_revision": 0},
    )
    assert answered.status_code == 200
    assert answered.json()["attempt_id"] == attempt_id
    assert answered.json()["status"] == "evaluating"
    assert "test-key" not in answered.text

    read_back = client.get(path, headers={"X-API-Key": "test-key"})
    assert read_back.status_code == 200
    assert read_back.json()["attempt_id"] == attempt_id
    assert read_back.json()["questions"][0]["answer"] == (
        "The answer mentions [redacted]."
    )
    stored = store._connection.execute(
        "SELECT questions_json FROM pbi_refinement_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert stored is not None
    assert "test-key" not in stored["questions_json"]

    wrong_project = client.get(
        "/projects/other/repositories/owner/api/pbis/64/refinement",
        headers={"X-API-Key": "test-key"},
    )
    assert wrong_project.status_code == 403
    wrong_repository = client.get(
        "/projects/project-1/repositories/owner/other/pbis/64/refinement",
        headers={"X-API-Key": "test-key"},
    )
    assert wrong_repository.status_code == 403
    writer_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={PROJECT_ID},
            workflow_actor="writer",
        )
    )
    wrong_role = writer_client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json={"questions": [{"text": "Another attempt?"}]},
    )
    assert wrong_role.status_code == 403
    assert writer_client.get(path, headers={"X-API-Key": "test-key"}).status_code == 403
    assert provider.discoveries == 0
    assert provider.handoffs == []
    store.close()
