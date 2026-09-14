import pytest

from beehaiive.contracts import (
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from tests.support.contracts.helpers import contract as contract


def test_pass_requires_evidence_and_required_artifact() -> None:
    required = contract(artifact=True)
    with pytest.raises(ContractError, match="Missing required evidence"):
        TaskResult(TaskOutcome.PASS, {}).validated(required)
    with pytest.raises(ContractError, match="Missing required artifacts"):
        TaskResult(TaskOutcome.PASS, {"summary": "done"}).validated(required)
    with pytest.raises(ContractError, match="undeclared artifact"):
        TaskResult(
            TaskOutcome.PASS,
            {"summary": "done"},
            ({"id": "other"},),
        ).validated(required)

    result = TaskResult(
        TaskOutcome.PASS,
        {"summary": "done"},
        ({"id": "report", "path": "report.txt"},),
    ).validated(required)
    assert result.artifact_refs == ({"id": "report", "path": "report.txt"},)


def test_pass_inventory_evidence_matches_contract_inputs() -> None:
    inventory = TaskContract.inventory(
        "owner/api", 32, "Inspect", branch="main", tracked_file_count=1
    )
    matching = {
        "repository": "owner/api",
        "branch": "main",
        "tracked_file_count": 1,
    }
    assert (
        TaskResult.from_payload(
            {"outcome": "pass", "evidence": matching, "artifact_refs": []}, inventory
        ).evidence
        == matching
    )

    for evidence in (
        {"repository": "other/repo", "branch": False, "tracked_file_count": -1},
        {"repository": "owner/api", "branch": "main", "tracked_file_count": True},
    ):
        with pytest.raises(
            ContractError, match="Evidence does not match contract inputs"
        ):
            TaskResult.from_payload(
                {"outcome": "pass", "evidence": evidence, "artifact_refs": []},
                inventory,
            )


def test_non_pass_results_have_terminal_requirements() -> None:
    required = contract(artifact=True)
    blocked = TaskResult(
        TaskOutcome.BLOCKED,
        {"summary": "needs access"},
        required_action="Grant repository access",
    ).validated(required)
    question = TaskResult(
        TaskOutcome.QUESTION,
        {"summary": "choice needed"},
        question="Which branch should be used?",
    ).validated(required)

    assert blocked.required_action == "Grant repository access"
    assert question.question == "Which branch should be used?"
    sanitized = TaskResult(
        TaskOutcome.QUESTION,
        {},
        question="Which branch should be used?",
        validation_reason="token=reason-secret",
        answer="token=answer-secret " + ("x" * 1_100),
    ).validated(required)
    assert sanitized.validation_reason == "token=[redacted]"
    assert sanitized.answer is not None
    assert "answer-secret" not in sanitized.answer
    assert len(sanitized.answer) <= 1_000
    with pytest.raises(ContractError, match="required_action"):
        TaskResult(TaskOutcome.BLOCKED, {}).validated(required)
    with pytest.raises(ContractError, match="require a question"):
        TaskResult(TaskOutcome.QUESTION, {}).validated(required)
    with pytest.raises(ContractError, match="Only question"):
        TaskResult(TaskOutcome.FAIL, {}, answer="not a question").validated(required)


def test_from_payload_rejects_bad_shape_and_round_trips_question() -> None:
    task_contract = contract()
    for value, message in (
        (None, "must be an object"),
        ({"outcome": 1}, "outcome must be a string"),
        ({"outcome": "unknown"}, "Unknown task outcome"),
        (
            {"outcome": "fail", "evidence": [], "artifact_refs": []},
            "evidence must be an object",
        ),
        (
            {"outcome": "fail", "evidence": {}, "artifact_refs": "report"},
            "artifact_refs must be a list",
        ),
        (
            {"outcome": "fail", "evidence": {}, "artifact_refs": ["report"]},
            "references must be objects",
        ),
        (
            {
                "outcome": "fail",
                "evidence": {},
                "artifact_refs": [],
                "unexpected": True,
            },
            "Unknown task result fields",
        ),
    ):
        with pytest.raises(ContractError, match=message):
            TaskResult.from_payload(value, task_contract)

    worker_question = {
        "outcome": "question",
        "evidence": {"summary": "choice needed"},
        "artifact_refs": [],
        "question": "Which branch?",
        "answer": "main",
    }
    with pytest.raises(ContractError, match="Worker task results"):
        TaskResult.from_payload(worker_question, task_contract)
    question = TaskResult.from_payload(
        worker_question,
        task_contract,
        allow_answer=True,
    )
    assert question.as_dict()["answer"] == "main"
    pass_with_artifact = TaskResult.from_payload(
        {
            "outcome": "pass",
            "evidence": {"summary": "done"},
            "artifact_refs": [{"id": "report"}],
        },
        contract(artifact=True),
    )
    assert pass_with_artifact.artifact_refs == ({"id": "report"},)
    assert TaskResult.invalid("token=secret").validation_reason == "token=[redacted]"
