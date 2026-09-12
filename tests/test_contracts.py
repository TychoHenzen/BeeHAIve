import math

import pytest

from beehaiive.contracts import (
    MAX_CONTRACT_ITEMS,
    MAX_RESULT_DEPTH,
    ArtifactRequirement,
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
)


def contract(*, artifact: bool = False) -> TaskContract:
    return TaskContract(
        "test.contract",
        1,
        "inspect",
        {"repository": "owner/api"},
        ("read_repository",),
        (ArtifactRequirement("report"),) if artifact else (),
        tuple(TaskOutcome),
        ("summary",),
    )


def test_contract_round_trip_and_inventory() -> None:
    inventory = TaskContract.inventory(
        "owner/api",
        32,
        "Inspect",
        branch="main",
        tracked_file_count=4,
        answer="use the existing report",
    )

    restored = TaskContract.from_dict(inventory.as_dict())

    assert restored == inventory
    assert restored.as_dict()["required_evidence"] == [
        "repository",
        "branch",
        "tracked_file_count",
    ]


def test_artifact_and_contract_validation_rejects_invalid_values() -> None:
    assert ArtifactRequirement.from_value({"id": "report"}).as_dict() == {
        "id": "report",
        "description": "",
        "required": True,
    }
    with pytest.raises(ContractError, match="Artifact requirements must be objects"):
        ArtifactRequirement.from_value("report")
    with pytest.raises(ContractError, match="Artifact required must be boolean"):
        ArtifactRequirement.from_value({"id": "report", "required": "yes"})
    with pytest.raises(ContractError, match="Artifact id must be a string"):
        ArtifactRequirement.from_value({"id": 1})
    with pytest.raises(ContractError, match="Artifact id is required"):
        ArtifactRequirement.from_value({"id": " "})
    with pytest.raises(ContractError, match="Artifact id is required"):
        ArtifactRequirement("")
    with pytest.raises(ContractError, match="Artifact id is too long"):
        ArtifactRequirement("x" * 1_001)
    with pytest.raises(ContractError, match="Artifact description is too long"):
        ArtifactRequirement("report", "x" * 1_001)
    with pytest.raises(ContractError, match="Contract version"):
        TaskContract.from_dict(
            {
                "contract_id": "test",
                "version": True,
                "step_id": "inspect",
                "inputs": {},
                "capabilities": [],
                "required_artifacts": [],
                "allowed_outcomes": ["pass"],
            }
        )
    with pytest.raises(ContractError, match="Contract version must be an integer"):
        TaskContract("test", True, "inspect", {}, (), (), (TaskOutcome.PASS,))
    with pytest.raises(ContractError, match="Contract version must be an integer"):
        TaskContract("test", 1.0, "inspect", {}, (), (), (TaskOutcome.PASS,))
    with pytest.raises(ContractError, match="Contract inputs"):
        TaskContract.from_dict(
            {
                "contract_id": "test",
                "version": 1,
                "step_id": "inspect",
                "inputs": [],
                "capabilities": [],
                "required_artifacts": [],
                "allowed_outcomes": ["pass"],
            }
        )
    with pytest.raises(ContractError, match="Contract inputs must be an object"):
        TaskContract("test", 1, "inspect", [], (), (), (TaskOutcome.PASS,))
    with pytest.raises(ContractError, match="Task contract must be an object"):
        TaskContract.from_dict(None)
    base = {
        "contract_id": "test",
        "version": 1,
        "step_id": "inspect",
        "inputs": {},
        "capabilities": [],
        "required_artifacts": [],
        "allowed_outcomes": ["pass"],
    }
    with pytest.raises(ContractError, match="Required artifacts must be a list"):
        TaskContract.from_dict({**base, "required_artifacts": "report"})
    with pytest.raises(ContractError, match="Allowed outcomes must be a list"):
        TaskContract.from_dict({**base, "allowed_outcomes": "pass"})
    with pytest.raises(ContractError, match="Allowed outcomes must be strings"):
        TaskContract.from_dict({**base, "allowed_outcomes": [1]})
    with pytest.raises(ContractError, match="Unknown task outcome"):
        TaskContract.from_dict({**base, "allowed_outcomes": ["unknown"]})
    with pytest.raises(ContractError, match="Contract capabilities must be a list"):
        TaskContract.from_dict({**base, "capabilities": "read"})

    with pytest.raises(ContractError, match="must be unique"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            ("read", "read"),
            (),
            (TaskOutcome.PASS,),
        )
    with pytest.raises(ContractError, match="at least one outcome"):
        TaskContract("test.contract", 1, "inspect", {}, (), (), ())
    with pytest.raises(ContractError, match="too many required artifacts"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            (),
            tuple(ArtifactRequirement(f"artifact-{index}") for index in range(21)),
            (TaskOutcome.PASS,),
        )
    with pytest.raises(ContractError, match="too many outcomes"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            (),
            (),
            tuple(TaskOutcome) + (TaskOutcome.PASS,),
        )
    with pytest.raises(ContractError, match="outcomes must be unique"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            (),
            (),
            (TaskOutcome.PASS, TaskOutcome.PASS),
        )
    with pytest.raises(ContractError, match="must contain non-empty strings"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            (),
            (),
            (TaskOutcome.PASS,),
            (" ",),
        )


def test_contract_bounds_and_secret_redaction() -> None:
    assert TaskResult(TaskOutcome.PASS, {"summary": 1.5}).validated(
        contract()
    ).evidence == {"summary": 1.5}
    value = TaskResult(
        TaskOutcome.PASS,
        {
            "summary": 'token: "secret" https://user:password@example.test',
        },
    ).validated(contract())

    serialized = value.as_dict()

    assert "secret" not in str(serialized)
    assert "[redacted]" in str(serialized)
    with pytest.raises(ContractError, match="nested too deeply"):
        TaskResult(
            TaskOutcome.PASS, {"summary": _nested(MAX_RESULT_DEPTH + 2)}
        ).validated(contract())
    with pytest.raises(ContractError, match="non-finite"):
        TaskResult(TaskOutcome.PASS, {"summary": math.inf}).validated(contract())
    with pytest.raises(ContractError, match="too many keys"):
        TaskResult(
            TaskOutcome.PASS,
            {"summary": {str(index): index for index in range(41)}},
        ).validated(contract())
    with pytest.raises(ContractError, match="keys must be non-empty"):
        TaskResult(TaskOutcome.PASS, {"summary": {1: "bad"}}).validated(contract())
    with pytest.raises(ContractError, match="too many items"):
        TaskResult(TaskOutcome.PASS, {"summary": list(range(21))}).validated(contract())


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


def test_result_requires_all_fields_and_validates_non_pass_artifacts() -> None:
    task_contract = contract(artifact=True)
    for field in ("evidence", "artifact_refs"):
        payload: dict[str, object] = {
            "outcome": "fail",
            "evidence": {},
            "artifact_refs": [],
        }
        del payload[field]
        with pytest.raises(ContractError, match="missing required fields"):
            TaskResult.from_payload(payload, task_contract)

    failure = TaskResult.from_payload(
        {
            "outcome": "fail",
            "evidence": {},
            "artifact_refs": [{"id": "report", "path": "report.txt"}],
        },
        task_contract,
    )
    assert failure.artifact_refs == ({"id": "report", "path": "report.txt"},)

    with pytest.raises(ContractError, match="undeclared artifact"):
        TaskResult.from_payload(
            {
                "outcome": "fail",
                "evidence": {},
                "artifact_refs": [{"id": "unknown"}],
            },
            task_contract,
        )


def test_contract_rejects_unsupported_and_oversized_values() -> None:
    with pytest.raises(ContractError, match="Unknown contract version"):
        TaskContract("test.contract", 2, "inspect", {}, (), (), (TaskOutcome.PASS,))
    with pytest.raises(ContractError, match="too many capabilities"):
        TaskContract(
            "test.contract",
            1,
            "inspect",
            {},
            tuple(f"cap-{index}" for index in range(MAX_CONTRACT_ITEMS + 1)),
            (),
            (TaskOutcome.PASS,),
        )
    with pytest.raises(ContractError, match="JSON-compatible"):
        TaskResult(TaskOutcome.PASS, {"summary": object()}).validated(contract())
    with pytest.raises(ContractError, match="Outcome is not allowed"):
        TaskResult(TaskOutcome.FAIL, {}).validated(
            TaskContract("test.contract", 1, "inspect", {}, (), (), (TaskOutcome.PASS,))
        )


def _nested(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value
