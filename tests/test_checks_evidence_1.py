from beehaiive.checks import (
    normalize_check_rollup,
)
from tests.support.checks.helpers import make_rollup as _rollup


def test_normalize_check_rollup_preserves_check_run_and_status_context_evidence() -> (
    None
):
    snapshot = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "CheckRun",
                "name": "python-tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "isRequired": True,
                "detailsUrl": "https://example.test/check",
                "startedAt": "2026-09-11T08:00:00Z",
                "completedAt": "2026-09-11T08:01:00Z",
            },
            {
                "__typename": "StatusContext",
                "context": "legacy-status",
                "state": "SUCCESS",
                "isRequired": False,
                "targetUrl": "https://example.test/status",
                "createdAt": "2026-09-11T08:00:00Z",
                "updatedAt": "2026-09-11T08:01:00Z",
            },
        ),
    )

    assert snapshot["verdict"] == "passing"
    assert snapshot["head_sha"] == "abc"
    assert snapshot["rollup_sha"] == "abc"
    assert snapshot["contexts"] == [
        {
            "kind": "check_run",
            "name": "python-tests",
            "required": True,
            "verdict": "passing",
            "status": "completed",
            "conclusion": "success",
            "url": "https://example.test/check",
            "started_at": "2026-09-11T08:00:00Z",
            "completed_at": "2026-09-11T08:01:00Z",
        },
        {
            "kind": "status_context",
            "name": "legacy-status",
            "required": False,
            "verdict": "passing",
            "state": "success",
            "url": "https://example.test/status",
            "created_at": "2026-09-11T08:00:00Z",
            "updated_at": "2026-09-11T08:01:00Z",
        },
    ]


def test_normalize_check_rollup_distinguishes_pending_blocking_and_unproven() -> None:
    pending = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "CheckRun",
                "name": "python-tests",
                "status": "IN_PROGRESS",
                "isRequired": True,
            }
        ),
    )
    blocking = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "StatusContext",
                "context": "codeql",
                "state": "FAILURE",
                "isRequired": True,
            }
        ),
    )
    unknown_requiredness = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "CheckRun",
                "name": "python-tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ),
    )
    mismatch = normalize_check_rollup("new", _rollup(oid="old"))

    assert pending["verdict"] == "pending"
    assert blocking["verdict"] == "blocking"
    assert unknown_requiredness["verdict"] == "unproven"
    assert mismatch["verdict"] == "unproven"


def test_normalize_missing_rollup_or_provider_error_is_unproven() -> None:
    missing_rollup = normalize_check_rollup("abc", None)
    provider_error = normalize_check_rollup(
        "abc", None, error="GitHub GraphQL rate limit exceeded"
    )

    assert missing_rollup["verdict"] == "unproven"
    assert provider_error == {
        "head_sha": "abc",
        "verdict": "unproven",
        "contexts": [],
        "error": "GitHub GraphQL rate limit exceeded",
    }


def test_normalize_unknown_context_and_malformed_nodes_are_unproven() -> None:
    unknown = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "ThirdPartyContext",
                "context": "unknown",
                "isRequired": True,
            }
        ),
    )
    malformed_nodes = normalize_check_rollup(
        "abc",
        {
            "commit": {"oid": "abc"},
            "contexts": {
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                    },
                    "not a context",
                ]
            },
        },
    )
    missing_nodes = normalize_check_rollup(
        "abc", {"commit": {"oid": "abc"}, "contexts": {"nodes": {}}}
    )
    optional_unknown = normalize_check_rollup(
        "abc",
        _rollup(
            {
                "__typename": "ThirdPartyContext",
                "context": "unknown",
                "isRequired": False,
            }
        ),
    )

    assert unknown["verdict"] == "unproven"
    assert malformed_nodes["verdict"] == "unproven"
    assert missing_nodes["verdict"] == "unproven"
    assert optional_unknown["verdict"] == "unproven"
