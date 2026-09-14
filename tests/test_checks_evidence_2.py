from beehaiive.checks import (
    aggregate_check_verdict,
    blocking_check_failure,
)


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
