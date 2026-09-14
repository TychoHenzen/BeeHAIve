import pytest

from beehaiive.contracts import (
    ArtifactRequirement,
    ContractError,
    TaskContract,
    TaskOutcome,
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
