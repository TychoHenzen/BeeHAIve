import pytest

from beehaiive.checks import (
    normalize_check_rollup,
)
from tests.support.checks.helpers import make_rollup as _rollup


@pytest.mark.parametrize(
    ("context", "expected"),
    (
        (
            {"__typename": "CheckRun", "status": "QUEUED", "isRequired": True},
            "pending",
        ),
        (
            {
                "__typename": "CheckRun",
                "status": "COMPLETED",
                "conclusion": "NEUTRAL",
                "isRequired": True,
            },
            "passing",
        ),
        (
            {
                "__typename": "CheckRun",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "isRequired": True,
            },
            "passing",
        ),
        (
            {
                "__typename": "CheckRun",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "isRequired": True,
            },
            "blocking",
        ),
        (
            {
                "__typename": "StatusContext",
                "state": "ERROR",
                "isRequired": True,
            },
            "blocking",
        ),
        (
            {"__typename": "StatusContext", "state": "PENDING", "isRequired": True},
            "pending",
        ),
        (
            {"__typename": "StatusContext", "state": "UNKNOWN", "isRequired": True},
            "unproven",
        ),
        (
            {"__typename": "CheckRun", "status": "UNKNOWN", "isRequired": True},
            "unproven",
        ),
        (
            {
                "__typename": "CheckRun",
                "status": "COMPLETED",
                "conclusion": "UNKNOWN",
                "isRequired": True,
            },
            "unproven",
        ),
    ),
)
def test_normalize_supported_github_check_outcomes(
    context: dict[str, object], expected: str
) -> None:
    snapshot = normalize_check_rollup("abc", _rollup(context))

    assert snapshot["verdict"] == expected
