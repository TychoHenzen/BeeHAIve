from beehaiive.pbi_readiness import project_pbi_readiness

OBSERVED_AT = "2026-09-14T08:00:00+00:00"


def _child(
    *,
    issue_state: str = "OPEN",
    state_reason: object = None,
    project_status: str | None = "Todo",
    blocked_by: list[dict[str, object]] | None = None,
    dependency_read_complete: bool = True,
    **extra: object,
) -> dict[str, object]:
    return {
        "issue_state": issue_state,
        "state_reason": state_reason,
        "project_status": project_status,
        "blocked_by": blocked_by or [],
        "dependency_read_complete": dependency_read_complete,
        "observed_at": OBSERVED_AT,
        **extra,
    }


def test_child_readiness_and_parent_aggregate_fail_closed() -> None:
    children, aggregate = project_pbi_readiness(
        [
            _child(),
            _child(project_status="In Progress"),
            _child(blocked_by=[{"number": 12, "state": "open", "state_reason": None}]),
            _child(
                issue_state="CLOSED",
                state_reason="NOT_PLANNED",
                project_status="Done",
            ),
            _child(
                issue_state="CLOSED",
                state_reason="COMPLETED",
                project_status="Done",
            ),
            _child(
                issue_state="CLOSED",
                state_reason="COMPLETED",
                project_status="Completed",
            ),
            _child(project_status=None),
            _child(project_status_conflict=True),
            _child(
                blocked_by=[
                    {"number": 13, "state": "closed", "state_reason": "completed"}
                ]
            ),
            _child(
                blocked_by=[
                    {"number": 14, "state": "closed", "state_reason": "not_planned"}
                ]
            ),
        ],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )

    assert [child["readiness"] for child in children] == [
        "ready",
        "incomplete",
        "blocked",
        "rejected",
        "completed",
        "unknown",
        "unknown",
        "unknown",
        "ready",
        "blocked",
    ]
    assert aggregate == {
        "status": "unknown",
        "counts": {
            "ready": 2,
            "incomplete": 1,
            "blocked": 2,
            "rejected": 1,
            "completed": 1,
            "unknown": 3,
        },
        "reasons": ["child_evidence_unknown"],
        "observed_at": OBSERVED_AT,
    }


def test_parent_aggregate_requires_a_complete_nonempty_child_relation() -> None:
    _, empty = project_pbi_readiness(
        [],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )
    _, incomplete = project_pbi_readiness(
        [_child()],
        relation_complete=False,
        relation_error="child_limit_exceeded",
        observed_at=OBSERVED_AT,
    )

    assert empty["status"] == "unknown"
    assert empty["reasons"] == ["no_linked_children"]
    assert incomplete["status"] == "unknown"
    assert incomplete["reasons"] == [
        "child_limit_exceeded",
        "child_relation_incomplete",
    ]


def test_closed_issue_without_project_status_uses_closed_reason() -> None:
    children, _ = project_pbi_readiness(
        [
            _child(
                issue_state="CLOSED",
                state_reason="COMPLETED",
                project_status=None,
            )
        ],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )

    assert children[0]["readiness"] == "completed"
    assert children[0]["readiness_reasons"] == []


def test_open_issue_with_duplicate_reason_is_unknown() -> None:
    children, _ = project_pbi_readiness(
        [_child(state_reason="DUPLICATE")],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )

    assert children[0]["readiness"] == "unknown"
    assert children[0]["readiness_reasons"] == ["open_issue_reason_conflict"]


def test_open_blocker_with_closed_reason_is_unknown() -> None:
    children, _ = project_pbi_readiness(
        [
            _child(
                blocked_by=[
                    {"number": 12, "state": "open", "state_reason": "COMPLETED"}
                ]
            )
        ],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )

    assert children[0]["readiness"] == "unknown"
    assert children[0]["readiness_reasons"] == ["blocker_state_reason_conflict"]


def test_missing_observation_time_makes_parent_unknown() -> None:
    _, aggregate = project_pbi_readiness(
        [_child()],
        relation_complete=True,
        relation_error=None,
        observed_at=None,
    )

    assert aggregate["status"] == "unknown"
    assert "observation_time_missing" in aggregate["reasons"]


def test_stale_or_missing_child_observation_is_unknown() -> None:
    children, _ = project_pbi_readiness(
        [_child(source_stale=True), _child(observed_at=None)],
        relation_complete=True,
        relation_error=None,
        observed_at=None,
    )

    assert [child["readiness"] for child in children] == ["unknown", "unknown"]
    assert children[0]["readiness_reasons"] == ["source_stale"]
    assert children[1]["readiness_reasons"] == ["observation_time_missing"]


def test_invalid_issue_state_reason_is_unknown() -> None:
    children, _ = project_pbi_readiness(
        [_child(state_reason=123)],
        relation_complete=True,
        relation_error=None,
        observed_at=OBSERVED_AT,
    )

    assert children[0]["readiness"] == "unknown"
    assert children[0]["readiness_reasons"] == ["state_reason_invalid"]


def test_parent_aggregate_precedence_matches_the_recorded_contract() -> None:
    rejected = _child(
        issue_state="CLOSED", state_reason="NOT_PLANNED", project_status="Done"
    )
    blocked = _child(blocked_by=[{"number": 12, "state": "open", "state_reason": None}])
    incomplete = _child(project_status="In Progress")
    completed = _child(
        issue_state="CLOSED", state_reason="COMPLETED", project_status="Done"
    )
    ready = _child()

    def status(*children: dict[str, object]) -> object:
        return project_pbi_readiness(
            list(children),
            relation_complete=True,
            relation_error=None,
            observed_at=OBSERVED_AT,
        )[1]["status"]

    assert status(rejected, blocked, incomplete) == "rejected"
    assert status(blocked, incomplete, completed) == "blocked"
    assert status(incomplete, completed) == "incomplete"
    assert status(completed, completed) == "completed"
    assert status(ready, completed) == "ready"
