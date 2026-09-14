from __future__ import annotations

import pytest

from beehaiive.models import ProjectSnapshot, RepositorySnapshot
from beehaiive.storage import OrchestratorStore, StoreError

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
