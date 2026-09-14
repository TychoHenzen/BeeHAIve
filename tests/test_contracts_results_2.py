import pytest

from beehaiive.contracts import (
    ContractError,
    TaskResult,
)
from tests.support.contracts.helpers import contract as contract


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
