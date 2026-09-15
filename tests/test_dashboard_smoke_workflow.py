import pytest

from beehaiive.provider import ITEMS_QUERY
from scripts.dashboard_smoke_proof import (
    live_terminal_pbi_proof,
    pull_request_line,
    rendered_pull_request_states,
    response_backed_dashboard_fields,
    response_metadata_lines,
)


def test_smoke_archive_proof_separates_default_and_archived_views() -> None:
    def archived_pbi(number: int) -> dict[str, object]:
        return {
            "number": number,
            "planning_status": "Done",
            "pull_requests": [{"number": number, "merged": True}],
            "stage_progress": [{"id": "merge", "status": "current"}],
        }

    default_payload = {
        "repositories": [{"name": "TychoHenzen/BeeHAIve", "pbis": [{"number": 1}]}]
    }
    default_snapshot = {
        "pbi_cards": [{"repository": "TychoHenzen/BeeHAIve", "number": "1"}]
    }
    archived_payload = {
        "repositories": [
            {
                "name": "TychoHenzen/BeeHAIve",
                "pbis": [archived_pbi(7), archived_pbi(8), archived_pbi(9)],
            }
        ]
    }
    archived_snapshot = {
        "pbi_cards": [
            {
                "repository": "TychoHenzen/BeeHAIve",
                "number": str(number),
                "text": "Project status: Done",
                "pull_requests": [f"#{number}: merged"],
            }
            for number in (7, 8, 9)
        ]
    }

    evidence = live_terminal_pbi_proof(
        default_payload, default_snapshot, archived_payload, archived_snapshot
    )

    assert evidence["7"]["default_projection"] == "omitted"
    assert evidence["7"]["archived_projection"] == "included"


def test_smoke_pull_request_formatter_matches_dashboard_evidence() -> None:
    assert pull_request_line(
        {
            "number": 9,
            "merged": True,
            "url": "https://example.test/pull/9",
            "source_branch": "codex/done",
            "source_branch_state": "deleted",
        }
    ) == ("#9: merged (https://example.test/pull/9, branch codex/done (deleted))")


def test_project_query_bounds_nested_connections() -> None:
    assert "labels(first: 20)" in ITEMS_QUERY
    assert "subIssues(first: 20)" in ITEMS_QUERY
    assert "comments(first: 20)" in ITEMS_QUERY
    assert (
        "closedByPullRequestsReferences(includeClosedPrs: true, "
        "first: 20)" in ITEMS_QUERY
    )
    assert "headRefName" in ITEMS_QUERY
    assert "headRef { name }" in ITEMS_QUERY
    assert "reviewRequests(first: 20)" in ITEMS_QUERY
    assert "latestReviews(first: 20)" in ITEMS_QUERY


def test_response_dashboard_fields_require_the_requested_project() -> None:
    fields = response_backed_dashboard_fields(
        {"project_id": "owner:8"},
        {},
        "owner:7",
    )

    assert fields["project_id"] is False


def test_response_metadata_lines_include_canonical_and_dependency_sections() -> None:
    lines = response_metadata_lines(
        {
            "repositories": [
                {
                    "pbis": [
                        {
                            "canonical_lifecycle": {
                                "reason_code": "advance",
                                "facts": {"project": {}, "provider": {}},
                                "transition_evidence": [
                                    {
                                        "state_before": "planning",
                                        "state_after": "refinement",
                                        "reason_code": "advance",
                                        "source_id": "owner/api#1",
                                        "observed_at": "now",
                                    }
                                ],
                            },
                            "subtasks": [{"id": "child", "title": "Check API"}],
                        }
                    ]
                }
            ]
        }
    )
    assert "Canonical lifecycle: Reason: advance" in lines
    assert "Canonical lifecycle: Facts: project, provider" in lines
    assert (
        "Subtasks: child: Check API. Readiness: unknown. Issue state unavailable. "
        "Project status unavailable. Blocked-by facts unavailable (read incomplete)."
        in lines
    )
    assert (
        "Canonical lifecycle: planning -> refinement · advance · source owner/api#1 · "
        "now"
    ) in lines
    assert "Dependency readiness: Unknown. Readiness evidence is unavailable." in lines
    multi_reason_lines = response_metadata_lines(
        {
            "repositories": [
                {
                    "pbis": [
                        {
                            "dependency_readiness": {
                                "status": "unknown",
                                "reasons": ["first", "second"],
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert "Dependency readiness: Reasons: first, second" in multi_reason_lines
    assert not any(
        line.startswith("Dependency readiness:")
        for line in response_metadata_lines(
            {"repositories": [{"pbis": [{"number": 1}]}]}
        )
    )


@pytest.mark.parametrize(
    ("pull_request", "rendered_line", "expected"),
    (
        pytest.param(
            {"number": 9, "state": "closed", "merged": True},
            "#9: merged",
            True,
            id="merged",
        ),
        pytest.param(
            {"number": 10, "state": "open"},
            "#10: open, review pending",
            True,
            id="open-review-pending",
        ),
        pytest.param(
            {
                "number": 11,
                "state": "closed",
                "review_decision": "changes_requested",
            },
            "#11: closed, changes_requested",
            True,
            id="closed-with-review-decision",
        ),
        pytest.param(
            {"number": 9, "state": "closed", "merged": True},
            "#9: review pending",
            False,
            id="wrong-state",
        ),
    ),
)
def test_pull_request_state_proof_uses_raw_state(
    pull_request: dict[str, object], rendered_line: str, expected: bool
) -> None:
    payload = {
        "repositories": [
            {
                "name": "owner/api",
                "pbis": [{"number": 1, "pull_requests": [pull_request]}],
            }
        ]
    }
    snapshot = {
        "pbi_cards": [
            {
                "repository": "owner/api",
                "number": "1",
                "pull_requests": [rendered_line],
            }
        ]
    }

    assert rendered_pull_request_states(payload, snapshot) is expected
