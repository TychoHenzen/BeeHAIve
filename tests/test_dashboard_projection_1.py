import pytest

from beehaiive.dashboard import build_dashboard_state


def test_dashboard_projection_exposes_autonomous_handoffs_from_actions() -> None:
    view = build_dashboard_state(
        {
            "project_id": "project-1",
            "name": "Planning",
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [{"number": 1, "title": "API one", "stage": "backlog"}],
                }
            ],
        },
        [
            {
                "kind": "skill:next-ticket",
                "repository": "owner/api",
                "pbi_number": 1,
                "result": {
                    "step": "next-ticket",
                    "status": "succeeded",
                    "summary": "Implemented the PBI.",
                    "handover": {"single_branch": True},
                },
            }
        ],
    )

    assert view["repositories"][0]["pbis"][0]["autonomous_handoffs"] == [
        {
            "step": "next-ticket",
            "status": "succeeded",
            "summary": "Implemented the PBI.",
            "handover": {"single_branch": True},
        }
    ]


def test_dashboard_projection_assigns_each_delivery_queue_from_live_evidence() -> None:
    def pull_request(review_decision: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {"number": 9, "state": "open"}
        if review_decision is not None:
            value["review_decision"] = review_decision
        return value

    state = {
        "project_id": "project-1",
        "name": "Planning",
        "repositories": [
            {
                "name": "owner/api",
                "pbis": [
                    {"number": 1, "title": "Backlog", "planning_status": "Backlog"},
                    {"number": 2, "title": "Todo", "planning_status": "Todo"},
                    {
                        "number": 3,
                        "title": "Publish",
                        "stage": "implement",
                        "planning_status": "In Progress",
                        "branch": "codex/3-publish",
                    },
                    {
                        "number": 4,
                        "title": "Review",
                        "stage": "pull_request",
                        "planning_status": "In Progress",
                        "metadata": {
                            "pull_requests": [pull_request()],
                            "checks": {"verdict": "passing"},
                        },
                    },
                    {
                        "number": 5,
                        "title": "Repair",
                        "stage": "pull_request",
                        "planning_status": "In Progress",
                        "metadata": {
                            "pull_requests": [pull_request("changes_requested")],
                            "checks": {"verdict": "passing"},
                        },
                    },
                    {
                        "number": 6,
                        "title": "Complete",
                        "stage": "pull_request",
                        "planning_status": "In Progress",
                        "metadata": {
                            "pull_requests": [
                                {**pull_request("approved"), "head_sha": "head-6"}
                            ],
                            "checks": {"verdict": "passing"},
                        },
                    },
                    {
                        "number": 7,
                        "title": "Blocked",
                        "stage": "implement",
                        "planning_status": "In Progress",
                        "status": "failed",
                    },
                    {
                        "number": 8,
                        "title": "Completed",
                        "planning_status": "Done",
                        "archived": True,
                    },
                ],
            }
        ],
    }

    view = build_dashboard_state(
        state,
        [
            {
                "kind": "skill:next-ticket",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 3,
                "result": {"status": "succeeded"},
            },
            {
                "kind": "skill:submit-draft-pr",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 4,
                "result": {"status": "published"},
            },
            {
                "kind": "skill:review-pr-branch",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 6,
                "result": {
                    "status": "succeeded",
                    "handover": {"pull_request": 9, "head": "head-6"},
                },
            },
        ],
    )

    queue_ids = [queue["id"] for queue in view["queues"]]
    assert queue_ids == [
        "refinement",
        "implementation",
        "publish",
        "review",
        "repair",
        "completion",
        "blocked",
        "completed",
    ]
    assert {queue["id"]: queue["count"] for queue in view["queues"]} == {
        "refinement": 1,
        "implementation": 1,
        "publish": 1,
        "review": 1,
        "repair": 1,
        "completion": 1,
        "blocked": 1,
        "completed": 0,
    }
    active = {pbi["number"]: pbi for pbi in view["repositories"][0]["pbis"]}
    assert active[1]["workflow_queue"]["id"] == "refinement"
    assert active[2]["workflow_queue"]["next_skill"] == "next-ticket"
    assert active[3]["workflow_queue"]["id"] == "publish"
    assert active[4]["workflow_queue"]["id"] == "review"
    assert active[5]["workflow_queue"]["id"] == "repair"
    assert active[6]["workflow_queue"]["id"] == "completion"
    assert active[7]["workflow_queue"]["id"] == "blocked"
    archived = build_dashboard_state(state, include_archived=True)
    assert {queue["id"]: queue["count"] for queue in archived["queues"]}[
        "completed"
    ] == 1


def test_old_review_head_does_not_complete_new_pull_request() -> None:
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
                            "title": "New pull request",
                            "stage": "pull_request",
                            "planning_status": "In Progress",
                            "metadata": {
                                "pull_requests": [
                                    {
                                        "number": 9,
                                        "state": "open",
                                        "head_sha": "new-head",
                                    }
                                ],
                                "checks": {"verdict": "passing"},
                            },
                        }
                    ],
                }
            ],
        },
        [
            {
                "kind": "skill:review-pr-branch",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
                "result": {
                    "status": "succeeded",
                    "handover": {
                        "pull_request": 9,
                        "head": "old-head",
                    },
                },
            }
        ],
    )

    pbi = view["repositories"][0]["pbis"][0]
    assert pbi["workflow_queue"]["id"] == "review"
    assert pbi["workflow_queue"]["reason"].startswith(
        "A published pull request is waiting"
    )


def test_provider_and_requeue_state_beat_old_actions() -> None:
    old_complete = {
        "kind": "skill:complete-pr",
        "status": "succeeded",
        "repository": "owner/api",
        "pbi_number": 1,
        "result": {
            "status": "succeeded",
            "handover": {
                "pull_request": 9,
                "head": "old-head",
            },
        },
    }
    state = {
        "project_id": "project-1",
        "name": "Planning",
        "repositories": [
            {
                "name": "owner/api",
                "pbis": [
                    {
                        "number": 1,
                        "title": "Still in Backlog",
                        "planning_status": "Backlog",
                        "archived": False,
                    }
                ],
            }
        ],
    }

    view = build_dashboard_state(state, [old_complete])
    pbi = view["repositories"][0]["pbis"][0]
    assert pbi["status"] == "idle"
    assert pbi["archived"] is False
    assert pbi["workflow_queue"]["id"] == "refinement"

    requeued = build_dashboard_state(
        {
            **state,
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [
                        {
                            "number": 1,
                            "title": "Requeued work",
                            "stage": "implement",
                            "planning_status": "In Progress",
                            "archived": False,
                        }
                    ],
                }
            ],
        },
        [
            {
                "kind": "requeue",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
            },
            old_complete,
        ],
    )
    assert requeued["repositories"][0]["pbis"][0]["workflow_queue"]["id"] == (
        "implementation"
    )


def test_dashboard_projection_requires_review_and_repair_evidence() -> None:
    open_pull_request = {
        "number": 9,
        "state": "open",
        "head_sha": "head-9",
        "review_decision": "approved",
    }
    base = {
        "project_id": "project-1",
        "name": "Planning",
        "repositories": [
            {
                "name": "owner/api",
                "pbis": [
                    {
                        "number": 1,
                        "title": "Needs evidence",
                        "stage": "pull_request",
                        "planning_status": "In Progress",
                        "metadata": {
                            "pull_requests": [open_pull_request],
                            "checks": {"verdict": "passing"},
                        },
                    }
                ],
            }
        ],
    }

    without_review = build_dashboard_state(base)
    assert without_review["repositories"][0]["pbis"][0]["workflow_queue"]["id"] == (
        "review"
    )
    unbound_review = build_dashboard_state(
        base,
        [
            {
                "kind": "skill:review-pr-branch",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
                "result": {"status": "succeeded", "handover": {}},
            }
        ],
    )
    assert unbound_review["repositories"][0]["pbis"][0]["workflow_queue"]["id"] == (
        "blocked"
    )
    no_checks = build_dashboard_state(
        {
            **base,
            "repositories": [
                {
                    "name": "owner/api",
                    "pbis": [
                        {
                            **base["repositories"][0]["pbis"][0],
                            "metadata": {"pull_requests": [open_pull_request]},
                        }
                    ],
                }
            ],
        }
    )
    assert no_checks["repositories"][0]["pbis"][0]["workflow_queue"]["id"] == (
        "blocked"
    )
    repair = build_dashboard_state(
        base,
        [
            {
                "kind": "skill:review-pr-branch",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
                "result": {
                    "status": "succeeded",
                    "handover": {
                        "pull_request": 9,
                        "head": "head-9",
                        "findings": [{"id": "one"}, {"id": "two"}],
                    },
                },
            }
        ],
    )
    evidence = repair["repositories"][0]["pbis"][0]["workflow_queue"]["evidence"]
    assert repair["repositories"][0]["pbis"][0]["workflow_queue"]["id"] == "repair"
    assert evidence["finding_count"] == 2
    assert evidence["finding_ids"] == ["one", "two"]
    assert evidence["all_findings_selected"] is True
    assert evidence["re_review"] is False


def test_dashboard_projection_overlays_autonomous_run_state_until_sync() -> None:
    state = {
        "project_id": "project-1",
        "name": "Planning",
        "repositories": [
            {
                "name": "owner/api",
                "pbis": [{"number": 1, "title": "API one", "stage": "backlog"}],
            }
        ],
    }
    pending = {
        "kind": "autonomous_start",
        "status": "pending",
        "repository": "owner/api",
        "pbi_number": 1,
        "run_id": "auto-1",
    }
    running = build_dashboard_state(state, [pending])
    assert running["repositories"][0]["pbis"][0]["status"] == "active"
    assert running["repositories"][0]["pbis"][0]["run_id"] == "auto-1"

    completed = build_dashboard_state(
        state,
        [
            {
                **pending,
                "status": "succeeded",
                "result": {"status": "completed", "summary": "Delivered."},
            }
        ],
    )
    completed_pbi = completed["repositories"][0]["pbis"][0]
    assert completed_pbi["status"] == "idle"
    assert completed_pbi["autonomous_status"] == "completed"
    assert completed_pbi["workflow_queue"]["id"] == "blocked"
    assert completed["recent_deliveries"][0]["pbi"]["result"] == "Delivered."


def test_provider_completion_clears_old_autonomous_failure() -> None:
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
                            "title": "Delivered work",
                            "stage": "implement",
                            "planning_status": "Done",
                            "archived": True,
                            "metadata": {
                                "issue_state": "CLOSED",
                                "pull_requests": [{"number": 9, "merged": True}],
                            },
                        }
                    ],
                }
            ],
        },
        [
            {
                "kind": "autonomous_start",
                "status": "failed",
                "repository": "owner/api",
                "pbi_number": 1,
                "run_id": "auto-1",
                "error": "old timeout",
            }
        ],
        include_archived=True,
    )

    pbi = view["repositories"][0]["pbis"][0]
    assert pbi["status"] == "completed"
    assert pbi["stage_label"] == "Merged"
    assert pbi["autonomous_status"] == "completed"
    assert pbi["last_error"] is None


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


def test_dashboard_projection_keeps_recent_deliveries_outside_the_active_queue() -> (
    None
):
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
                            "title": "Delivered work",
                            "stage": "merge",
                            "status": "idle",
                            "archived": True,
                            "result": "Merged",
                        }
                    ],
                }
            ],
        }
    )

    assert view["repositories"][0]["pbis"] == []
    assert view["recent_deliveries"][0]["pbi"]["title"] == "Delivered work"


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
