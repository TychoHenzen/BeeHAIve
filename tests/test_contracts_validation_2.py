import math

import pytest

from beehaiive.contracts import (
    MAX_CONTRACT_ITEMS,
    MAX_RESULT_DEPTH,
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
)
from tests.support.contracts.helpers import contract as contract
from tests.support.contracts.helpers import nested_value as _nested


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
