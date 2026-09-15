import pytest

from beehaiive.dashboard import build_dashboard_state


def test_dashboard_projection_exposes_optional_run_details() -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "updated_at": "now",
            "event_limit": 100,
            "repositories": [
                {
                    "name": "owner/api",
                    "active": True,
                    "writer": {"run_id": "run-1", "pbi_number": 1},
                    "pbis": [
                        {
                            "id": "owner/api#1",
                            "number": 1,
                            "title": "API one",
                            "stage": "pull_request",
                            "status": "active",
                            "run_id": "run-1",
                            "task_contract": {
                                "contract_id": "test.contract",
                                "version": 1,
                                "step_id": "inspect",
                            },
                            "task_result": {
                                "outcome": "pass",
                                "evidence": {"summary": "done"},
                                "artifact_refs": [{"id": "report"}],
                            },
                            "agent_session": {
                                "session_id": "run-1",
                                "events": [{"sequence": 1, "text": "started"}],
                            },
                            "events": [
                                {
                                    "type": "review",
                                    "details": {
                                        "subtasks": [
                                            {"id": "1a", "title": "Check API"}
                                        ],
                                        "readers": [
                                            {"id": "security", "status": "pass"},
                                            {"id": "tests", "status": "pending"},
                                        ],
                                        "reviewers": {
                                            "security": {"status": "pass"},
                                            "tests": {"status": "pending"},
                                        },
                                        "escalation": {
                                            "current": 1,
                                            "consecutive": 2,
                                            "current_tier": "terra",
                                        },
                                        "escalation_log": [{"tier": "terra"}],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    counts = view["counts"]
    pbi = view["repositories"][0]["pbis"][0]
    assert counts == {
        "projects": 1,
        "repositories": 1,
        "active_repositories": 1,
        "pbis": 1,
        "subtasks": 1,
        "writers": 1,
        "readers": 2,
        "active_runs": 1,
        "awaiting_operator_runs": 0,
        "failed_runs": 0,
        "completed_runs": 0,
    }
    assert pbi["stage_label"] == "Review"
    assert pbi["subtasks"] == [{"id": "1a", "title": "Check API"}]
    assert pbi["escalation"] == {
        "current": 1,
        "consecutive": 2,
        "current_tier": "terra",
    }
    assert pbi["escalation_log"] == [{"tier": "terra"}]
    assert pbi["task_contract"]["contract_id"] == "test.contract"
    assert pbi["task_result"]["artifact_refs"] == [{"id": "report"}]
    assert pbi["agent_session"]["session_id"] == "run-1"
    assert pbi["agent_session"]["events"][0]["sequence"] == 1
    assert pbi["canonical_lifecycle"] == {
        "state": "unknown",
        "facts": {},
        "source_version": "",
        "reason_code": "evidence_missing",
        "required_action": None,
        "transition_evidence": [],
    }

    completed = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [
                        {
                            "number": 1,
                            "stage": "pull_request",
                            "status": "completed",
                        }
                    ],
                }
            ],
        }
    )
    assert completed["repositories"][0]["pbis"][0]["stage_label"] == "Pull request"

    reviewer_only = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "updated_at": "now",
            "event_limit": 100,
            "repositories": [
                {
                    "name": "owner/api",
                    "active": True,
                    "writer": None,
                    "pbis": [
                        {
                            "number": 2,
                            "stage": "backlog",
                            "events": [
                                {
                                    "type": "review",
                                    "details": {
                                        "reviewers": {"security": {"status": "pass"}}
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    assert reviewer_only["counts"]["readers"] == 1
    assert reviewer_only["repositories"][0]["pbis"][0]["readers"] == [
        {"id": "security", "status": "pass"}
    ]

    empty = build_dashboard_state(
        {"project_id": "project-1", "name": "Planning", "repositories": None}
    )
    assert empty["repositories"] == []


def test_dashboard_projection_preserves_canonical_lifecycle_evidence() -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [
                        {
                            "number": 1,
                            "canonical_lifecycle": {
                                "state": "blocked",
                                "facts": {"provider": {"checks_verdict": "blocking"}},
                                "source_version": "v1",
                                "reason_code": "conflict",
                                "required_action": "Refresh checks",
                                "transition_evidence": [
                                    {
                                        "state_before": "checks",
                                        "state_after": "blocked",
                                        "reason_code": "conflict",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    )
    canonical = view["repositories"][0]["pbis"][0]["canonical_lifecycle"]
    assert canonical["state"] == "blocked"
    assert canonical["facts"] == {"provider": {"checks_verdict": "blocking"}}
    assert canonical["required_action"] == "Refresh checks"
    assert canonical["transition_evidence"][0]["state_after"] == "blocked"


@pytest.mark.parametrize(
    ("raw_pbi", "expected_status", "expected_stage_label"),
    (
        pytest.param(
            {
                "number": 1,
                "stage": "backlog",
                "planning_status": "Done",
                "metadata": {
                    "pull_requests": [{"number": 9, "state": "closed", "merged": True}]
                },
            },
            "idle",
            "Merged",
            id="done-with-merged-pull-request",
        ),
        pytest.param(
            {"number": 2, "stage": "backlog", "planning_status": "Blocked"},
            "idle",
            "Blocked",
            id="blocked",
        ),
        pytest.param(
            {"number": 3, "stage": "backlog", "planning_status": "New status"},
            "idle",
            "New status",
            id="unknown-status",
        ),
        pytest.param(
            {"number": 4, "stage": "implement", "planning_status": "Backlog"},
            "idle",
            "Backlog",
            id="backlog",
        ),
        pytest.param(
            {"number": 5, "stage": "pull_request", "planning_status": "Todo"},
            "idle",
            "Refine",
            id="todo",
        ),
        pytest.param(
            {
                "number": 6,
                "stage": "backlog",
                "planning_status": "In Progress",
            },
            "idle",
            "Implement",
            id="in-progress",
        ),
        pytest.param(
            {
                "number": 7,
                "stage": "implement",
                "status": "active",
                "planning_status": "Done",
            },
            "active",
            "Implement",
            id="active-run",
        ),
        pytest.param(
            {
                "number": 8,
                "stage": "implement",
                "status": "failed",
                "planning_status": "Blocked",
            },
            "failed",
            "Implement",
            id="failed-run",
        ),
        pytest.param(
            {
                "number": 9,
                "stage": "pull_request",
                "status": "completed",
                "planning_status": "Done",
            },
            "completed",
            "Pull request",
            id="completed-run",
        ),
        pytest.param(
            {"number": 10, "stage": "implement", "planning_status": "Done"},
            "idle",
            "Done",
            id="done-without-pull-request",
        ),
    ),
)
def test_dashboard_projection_separates_project_and_local_statuses(
    raw_pbi: dict[str, object],
    expected_status: str,
    expected_stage_label: str,
) -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [raw_pbi],
                }
            ],
        }
    )

    pbi = view["repositories"][0]["pbis"][0]
    assert pbi["status"] == expected_status
    assert pbi["stage_label"] == expected_stage_label
    assert pbi["planning_status"] == raw_pbi["planning_status"]
