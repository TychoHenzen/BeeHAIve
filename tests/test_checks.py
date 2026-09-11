import pytest

from beehaiive.checks import (
    aggregate_check_verdict,
    blocking_check_failure,
    normalize_check_rollup,
)


def _rollup(*contexts: dict[str, object], oid: str = "abc") -> dict[str, object]:
    return {
        "commit": {"oid": oid},
        "state": "SUCCESS",
        "contexts": {"nodes": list(contexts)},
    }


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
            "contexts": {"nodes": {"not": "a list"}},
        },
    )

    assert unknown["verdict"] == "unproven"
    assert malformed_nodes["verdict"] == "unproven"


def test_aggregate_and_blocking_failure_use_current_required_evidence() -> None:
    assert aggregate_check_verdict([]) == "unproven"
    assert aggregate_check_verdict([{"verdict": "passing"}]) == "passing"
    assert (
        aggregate_check_verdict([{"verdict": "passing"}, {"verdict": "pending"}])
        == "pending"
    )
    assert (
        aggregate_check_verdict([{"verdict": "blocking"}, {"verdict": "unproven"}])
        == "unproven"
    )
    assert aggregate_check_verdict([{"verdict": "blocking"}]) == "blocking"
    assert aggregate_check_verdict([{}]) == "unproven"

    assert blocking_check_failure({"pull_requests": "not a list"}) is None
    assert (
        blocking_check_failure(
            {
                "pull_requests": [
                    {"number": 7, "state": "open", "checks": {"verdict": "passing"}}
                ]
            }
        )
        is None
    )
    assert (
        blocking_check_failure(
            {
                "pull_requests": [
                    {
                        "number": 7,
                        "state": "open",
                        "checks": {"verdict": "blocking", "blocking_contexts": []},
                    }
                ]
            }
        )
        is None
    )

    error = blocking_check_failure(
        {
            "pull_requests": [
                {
                    "number": 7,
                    "state": "open",
                    "checks": {
                        "head_sha": "abc",
                        "verdict": "blocking",
                        "blocking_contexts": [
                            {
                                "name": "codeql",
                                "state": "failure",
                                "required": True,
                                "verdict": "blocking",
                                "url": "https://example.test/codeql",
                            }
                        ],
                    },
                }
            ]
        }
    )

    assert error == (
        "Pull request #7 check 'codeql' blocks head abc: failure "
        "(https://example.test/codeql)"
    )
