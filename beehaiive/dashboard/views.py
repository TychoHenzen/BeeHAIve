from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from .stages import display_stage, display_stage_label, stage_progress
from .values import integer, mapping, mappings, sequence

_TERMINAL_PLANNING_STATUSES = frozenset({"closed", "completed", "done", "merged"})

_QUEUE_SPECS: tuple[tuple[str, str, str | None], ...] = (
    ("refinement", "Refinement", "refine-backlog-item"),
    ("implementation", "Implementation", "next-ticket"),
    ("publish", "Publish", "submit-draft-pr"),
    ("review", "Review", "review-pr-branch"),
    ("repair", "Repair", "fix-pr-review"),
    ("completion", "Completion", "complete-pr"),
    ("blocked", "Blocked", "codex-advisor"),
    ("completed", "Completed", None),
)
_QUEUE_DEFINITIONS = {
    queue_id: {"id": queue_id, "label": label, "owner": owner, "next_skill": owner}
    for queue_id, label, owner in _QUEUE_SPECS
}
_SUCCESSFUL_STATUSES = frozenset(
    {
        "complete",
        "completed",
        "done",
        "fixed",
        "merged",
        "passed",
        "published",
        "reviewed",
        "success",
        "succeeded",
    }
)
_SKILL_QUEUE_IDS = {
    "refine-backlog-item": "refinement",
    "next-ticket": "implementation",
    "submit-draft-pr": "publish",
    "review-pr-branch": "review",
    "fix-pr-review": "repair",
    "complete-pr": "completion",
}


def queue_definitions() -> list[dict[str, object]]:
    return [dict(value) for value in _QUEUE_DEFINITIONS.values()]


def _queue_result(
    queue_id: str, reason: str, evidence: Mapping[str, object]
) -> dict[str, object]:
    return {
        **_QUEUE_DEFINITIONS[queue_id],
        "reason": reason[:500],
        "evidence": dict(evidence),
    }


def queue_for_skill(skill: object, status: object = "running") -> dict[str, object]:
    skill_name = str(skill)
    queue_id = _SKILL_QUEUE_IDS.get(skill_name, "blocked")
    return _queue_result(
        queue_id,
        "Autonomous skill " + skill_name + " is " + str(status) + ".",
        {"skill": skill_name, "status": str(status)},
    )


def _latest_skill(
    actions: Sequence[Mapping[str, object]], name: str
) -> Mapping[str, object] | None:
    for action in actions:
        if action.get("kind") == "skill:" + name:
            return action
    return None


def _handoff_matches_pull_request(
    action: Mapping[str, object] | None,
    pull_requests: Sequence[Mapping[str, object]],
) -> bool:
    if action is None:
        return True
    if not pull_requests:
        return False
    current = pull_requests[-1]
    handover = mapping(mapping(action.get("result")).get("handover"))
    expected_number = handover.get("pull_request") or handover.get("pr")
    actual_number = current.get("number")
    expected_number_text = str(expected_number).strip()
    actual_number_text = str(actual_number).strip()
    if not expected_number_text or not actual_number_text:
        return False
    if expected_number_text != actual_number_text:
        return False
    expected_head = handover.get("head") or handover.get("head_commit")
    actual_head = current.get("head_sha")
    return (
        isinstance(expected_head, str)
        and bool(expected_head.strip())
        and isinstance(actual_head, str)
        and bool(actual_head.strip())
        and expected_head == actual_head
    )


def _handoff_is_bound(action: Mapping[str, object] | None) -> bool:
    if action is None:
        return False
    handover = mapping(mapping(action.get("result")).get("handover"))
    number = handover.get("pull_request") or handover.get("pr")
    head = handover.get("head") or handover.get("head_commit")
    number_bound = (type(number) is int and number > 0) or (
        isinstance(number, str) and number.strip().isdigit() and int(number.strip()) > 0
    )
    return number_bound and isinstance(head, str) and bool(head.strip())


def _successful(action: Mapping[str, object] | None) -> bool:
    if action is None:
        return False
    if action.get("status") == "succeeded":
        return True
    return (
        str(mapping(action.get("result")).get("status") or "").casefold()
        in _SUCCESSFUL_STATUSES
    )


def _has_findings(action: Mapping[str, object] | None) -> bool:
    if action is None:
        return False
    result = mapping(action.get("result"))
    for source in (result, mapping(result.get("handover"))):
        for key in ("findings", "review_findings"):
            value = source.get(key)
            if (
                isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray))
                and value
            ):
                return True
    return False


def _finding_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    finding = cast(Mapping[str, object], value)
    raw_id = finding.get("id") or finding.get("finding_id")
    return str(raw_id) if raw_id is not None else None


def _repair_evidence(action: Mapping[str, object]) -> dict[str, object]:
    result = mapping(action.get("result"))
    values: object = None
    for source in (result, mapping(result.get("handover"))):
        values = source.get("findings") or source.get("review_findings")
        if isinstance(values, Sequence) and not isinstance(
            values, (str, bytes, bytearray)
        ):
            break
    findings = (
        list(cast(Sequence[object], values))
        if isinstance(values, Sequence)
        and not isinstance(values, (str, bytes, bytearray))
        else []
    )
    finding_ids = [
        finding_id
        for finding in findings
        if (finding_id := _finding_id(finding)) is not None
    ]
    return {
        "skill": "fix-pr-review",
        "finding_count": len(findings),
        "finding_ids": finding_ids[:100],
        "all_findings_selected": True,
        "re_review": False,
    }


def queue_for_pbi(
    status: str,
    planning_status: object,
    stage: str,
    archived: bool,
    branch: object,
    pull_requests: Sequence[object],
    checks: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
    canonical: Mapping[str, object],
) -> dict[str, object]:
    normalized_planning = (
        planning_status.strip().casefold() if isinstance(planning_status, str) else ""
    )
    normalized_status = status.casefold()
    check_verdict = str(checks.get("verdict") or "").casefold()
    canonical_state = str(canonical.get("state") or "").casefold()
    open_pull_requests = [
        mapping(value)
        for value in pull_requests
        if str(mapping(value).get("state") or "").casefold() == "open"
    ]
    if (
        archived
        or normalized_planning in _TERMINAL_PLANNING_STATUSES
        or any(mapping(value).get("merged") is True for value in pull_requests)
    ):
        return _queue_result(
            "completed",
            "Provider completion evidence is confirmed.",
            {"planning_status": planning_status, "pull_requests": len(pull_requests)},
        )
    if (
        normalized_status in {"failed", "awaiting_operator"}
        or normalized_planning == "blocked"
        or check_verdict == "blocking"
        or canonical_state in {"blocked", "human_action_required"}
    ):
        return _queue_result(
            "blocked",
            "The current work requires an operator or safe recovery action.",
            {
                "status": status,
                "planning_status": planning_status,
                "checks": check_verdict or "unavailable",
            },
        )

    if normalized_status not in {"active", "awaiting_operator"}:
        if normalized_planning == "backlog":
            return _queue_result(
                "refinement",
                "Project Status is Backlog and the PBI needs refinement.",
                {"planning_status": planning_status},
            )
        if normalized_planning == "todo":
            return _queue_result(
                "implementation",
                "Project Status is Todo and the PBI is ready for implementation.",
                {"planning_status": planning_status},
            )

    draft_pull_request = next(
        (
            pull_request
            for pull_request in open_pull_requests
            if any(
                pull_request.get(key) is True
                for key in ("is_draft", "draft", "isDraft")
            )
        ),
        None,
    )
    if draft_pull_request is not None:
        return _queue_result(
            "publish",
            "The pull request is still a draft and needs publication.",
            {
                "skill": "submit-draft-pr",
                "pull_request": draft_pull_request.get("number"),
                "draft": True,
            },
        )

    complete = _latest_skill(actions, "complete-pr")
    if complete is not None and not _handoff_is_bound(complete):
        return _queue_result(
            "blocked",
            "Completion evidence is missing its pull request and head binding.",
            {"skill": "complete-pr"},
        )
    if complete is not None and not open_pull_requests:
        return _queue_result(
            "blocked",
            "Completion evidence has no current pull request to verify.",
            {"skill": "complete-pr"},
        )
    if not _handoff_matches_pull_request(complete, open_pull_requests):
        complete = None
    if complete is not None:
        if check_verdict != "passing":
            return _queue_result(
                "blocked",
                "Completion evidence has no passing check result for the "
                "current pull request.",
                {"skill": "complete-pr", "checks": check_verdict or "unavailable"},
            )
        return _queue_result(
            "completion" if _successful(complete) else "blocked",
            "Completion handoff succeeded; provider reconciliation is pending."
            if _successful(complete)
            else "The pull request completion handoff did not complete.",
            {"skill": "complete-pr"},
        )
    fixed = _latest_skill(actions, "fix-pr-review")
    if fixed is not None and not _handoff_is_bound(fixed):
        return _queue_result(
            "blocked",
            "Repair evidence is missing its pull request and head binding.",
            {"skill": "fix-pr-review"},
        )
    if fixed is not None and not open_pull_requests:
        return _queue_result(
            "blocked",
            "Repair evidence has no current pull request to verify.",
            {"skill": "fix-pr-review"},
        )
    if not _handoff_matches_pull_request(fixed, open_pull_requests):
        fixed = None
    if fixed is not None:
        if check_verdict != "passing":
            return _queue_result(
                "blocked",
                "Repair evidence has no passing check result for the "
                "current pull request.",
                {"skill": "fix-pr-review", "checks": check_verdict or "unavailable"},
            )
        return _queue_result(
            "completion" if _successful(fixed) else "blocked",
            "Review repair is complete; the pull request needs completion."
            if _successful(fixed)
            else "The review repair handoff did not complete.",
            {"skill": "complete-pr" if _successful(fixed) else "fix-pr-review"},
        )
    review = _latest_skill(actions, "review-pr-branch")
    if review is not None and not _handoff_is_bound(review):
        return _queue_result(
            "blocked",
            "Review evidence is missing its pull request and head binding.",
            {"skill": "review-pr-branch"},
        )
    if review is not None and not open_pull_requests:
        return _queue_result(
            "blocked",
            "Review evidence has no current pull request to verify.",
            {"skill": "review-pr-branch"},
        )
    if not _handoff_matches_pull_request(review, open_pull_requests):
        review = None
    if review is not None:
        if not open_pull_requests:
            return _queue_result(
                "blocked",
                "Review evidence has no current pull request to verify.",
                {"skill": "review-pr-branch"},
            )
        if not _successful(review):
            return _queue_result(
                "blocked",
                "The branch review handoff did not complete.",
                {"skill": "review-pr-branch"},
            )
        if check_verdict != "passing":
            return _queue_result(
                "blocked",
                "Review evidence has no passing check result for the "
                "current pull request.",
                {"skill": "review-pr-branch", "checks": check_verdict or "unavailable"},
            )
        decision = str(open_pull_requests[-1].get("review_decision") or "").casefold()
        if decision in {"changes_requested", "request_changes"}:
            return _queue_result(
                "blocked",
                "The provider requested changes without matching branch-review "
                "findings.",
                {"skill": "review-pr-branch", "review_decision": decision},
            )
        if _has_findings(review):
            return _queue_result(
                "repair",
                "The branch review recorded findings that need repair.",
                _repair_evidence(review),
            )
        return _queue_result(
            "completion",
            "Review evidence is complete; the pull request needs completion.",
            {"skill": "complete-pr"},
        )
    if open_pull_requests:
        if check_verdict in {"", "unproven", "unknown"}:
            return _queue_result(
                "blocked",
                "The current pull request has no proven check result.",
                {"checks": check_verdict or "unavailable"},
            )
        pull_request = open_pull_requests[-1]
        decision = str(pull_request.get("review_decision") or "").casefold()
        if decision in {"changes_requested", "request_changes"}:
            return _queue_result(
                "review",
                "The published pull request needs the required branch review "
                "before provider changes can be repaired.",
                {
                    "skill": "review-pr-branch",
                    "review_decision": decision,
                },
            )
        return _queue_result(
            "review",
            (
                "The pull request is approved and checks pass, but the branch "
                "review handoff is not recorded."
                if decision == "approved" and check_verdict == "passing"
                else "A published pull request is waiting for branch review."
            ),
            {
                "pull_requests": len(open_pull_requests),
                "review_decision": decision or "pending",
            },
        )
    submitted = _latest_skill(actions, "submit-draft-pr")
    if submitted is not None:
        if not _handoff_is_bound(submitted):
            return _queue_result(
                "blocked",
                "Publication evidence is missing its pull request and head binding.",
                {"skill": "submit-draft-pr"},
            )
        if not open_pull_requests:
            return _queue_result(
                "blocked",
                "Publication evidence has no current pull request to verify.",
                {"skill": "submit-draft-pr"},
            )
        submitted_handover = mapping(mapping(submitted.get("result")).get("handover"))
        if any(
            submitted_handover.get(key) is True
            for key in ("draft", "is_draft", "isDraft")
        ):
            return _queue_result(
                "publish",
                "The publication handoff still reports a draft pull request.",
                {"skill": "submit-draft-pr", "draft": True},
            )
        return _queue_result(
            "review" if _successful(submitted) else "blocked",
            "The pull request was published and is waiting for review."
            if _successful(submitted)
            else "The pull request publication handoff did not complete.",
            {
                "skill": "review-pr-branch"
                if _successful(submitted)
                else "submit-draft-pr"
            },
        )
    implemented = _latest_skill(actions, "next-ticket")
    if implemented is not None:
        handover = mapping(mapping(implemented.get("result")).get("handover"))
        has_branch = bool(
            branch or handover.get("branch") or handover.get("workspace_branch")
        )
        return _queue_result(
            "publish" if _successful(implemented) and has_branch else "blocked",
            (
                "Implementation evidence is present without a published pull request."
                if _successful(implemented) and has_branch
                else "Implementation evidence is missing its branch binding."
                if _successful(implemented)
                else "The implementation handoff did not complete."
            ),
            {
                "skill": "submit-draft-pr"
                if _successful(implemented) and has_branch
                else "next-ticket",
                "branch": has_branch,
            },
        )
    refined = _latest_skill(actions, "refine-backlog-item")
    if refined is not None:
        return _queue_result(
            "implementation" if _successful(refined) else "blocked",
            "Refinement evidence is present; the PBI is ready for implementation."
            if _successful(refined)
            else "The refinement handoff did not complete.",
            {"skill": "next-ticket" if _successful(refined) else "refine-backlog-item"},
        )
    if normalized_planning == "backlog" or stage == "refine":
        return _queue_result(
            "refinement",
            "Project Status is Backlog and the PBI needs refinement.",
            {"planning_status": planning_status},
        )
    if normalized_planning == "todo":
        return _queue_result(
            "implementation",
            "Project Status is Todo and the PBI is ready for implementation.",
            {"planning_status": planning_status},
        )
    if branch:
        return _queue_result(
            "publish",
            "Implementation has a branch but no published pull request evidence.",
            {"branch": bool(branch), "stage": stage},
        )
    if stage in {"pull_request", "review"}:
        return _queue_result(
            "blocked",
            "The pull-request stage has no branch or pull-request evidence.",
            {"branch": False, "stage": stage},
        )
    if normalized_planning == "in progress" or stage == "implement":
        return _queue_result(
            "implementation",
            "The PBI is in active implementation.",
            {"planning_status": planning_status, "stage": stage},
        )
    return _queue_result(
        "blocked",
        "The PBI has no recognized lifecycle evidence.",
        {"planning_status": planning_status, "stage": stage},
    )


def _provider_completion_confirmed(
    planning_status: object,
    issue_state: object,
    pull_requests: Sequence[object],
) -> bool:
    normalized_planning_status = (
        planning_status.strip().casefold() if isinstance(planning_status, str) else ""
    )
    normalized_issue_state = (
        issue_state.strip().casefold() if isinstance(issue_state, str) else ""
    )
    return (
        normalized_planning_status in _TERMINAL_PLANNING_STATUSES
        or normalized_issue_state == "closed"
        or any(
            mapping(pull_request).get("merged") is True
            for pull_request in pull_requests
        )
    )


def repository_view(
    raw_repository: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
    include_archived: bool,
) -> dict[str, object]:
    raw_writer = mapping(raw_repository.get("writer"))
    if raw_writer:
        writer: dict[str, object] = {
            **raw_writer,
            "status": "active",
            "current_pbi": raw_writer.get("pbi_number"),
        }
    else:
        writer = {"status": "idle", "current_pbi": None}

    pbis: list[dict[str, object]] = []
    for raw_pbi in mappings(raw_repository.get("pbis")):
        pbi = pbi_view(raw_pbi, raw_repository.get("name"), actions)
        if bool(pbi.get("archived")) is include_archived:
            pbis.append(pbi)
    return {
        "name": raw_repository.get("name"),
        "active": bool(raw_repository.get("active")),
        "writer": writer,
        "pbis": pbis,
        "counts": {
            "pbis": len(pbis),
            "subtasks": sum(len(sequence(pbi.get("subtasks"))) for pbi in pbis),
            "readers": sum(
                len(sequence(pbi.get("readers"))) or len(mapping(pbi.get("reviewers")))
                for pbi in pbis
            ),
        },
    }


def pbi_view(
    raw_pbi: Mapping[str, object],
    repository_name: object,
    actions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    events = mappings(raw_pbi.get("events"))
    metadata = dict(mapping(raw_pbi.get("metadata")))
    metadata.update(latest_metadata(events))
    raw_stage = str(raw_pbi.get("stage") or "backlog")
    raw_status = raw_pbi.get("status")
    status = str(raw_status) if raw_status is not None else "idle"
    planning_status = raw_pbi.get("planning_status")
    checks = dict(mapping(metadata.get("checks")))
    pull_requests = sequence(metadata.get("pull_requests"))
    readers = sequence(metadata.get("readers"))
    reviewers = dict(mapping(metadata.get("reviewers")))
    if not readers and reviewers:
        readers = [
            {"id": reviewer_id, **mapping(reviewer)}
            for reviewer_id, reviewer in reviewers.items()
        ]
    escalation = escalation_view(metadata, events)
    subtasks = sequence(metadata.get("subtasks"))
    dependency_readiness = mapping(metadata.get("dependency_readiness")) or None
    source_url = metadata.get("source_url")
    canonical = mapping(raw_pbi.get("canonical_lifecycle"))
    canonical_state = canonical.get("state")
    canonical_reason = canonical.get("reason_code")
    canonical_source_version = canonical.get("source_version")
    required_action = canonical.get("required_action")
    canonical_lifecycle = {
        "state": canonical_state if isinstance(canonical_state, str) else "unknown",
        "facts": dict(mapping(canonical.get("facts"))),
        "source_version": (
            canonical_source_version
            if isinstance(canonical_source_version, str)
            else ""
        ),
        "reason_code": (
            canonical_reason
            if isinstance(canonical_reason, str)
            else "evidence_missing"
        ),
        "required_action": (
            required_action if isinstance(required_action, str) else None
        ),
        "transition_evidence": mappings(canonical.get("transition_evidence")),
    }
    matching_actions = [
        dict(action)
        for action in actions
        if action_matches(action, repository_name, raw_pbi)
    ]
    status = str(raw_status) if raw_status is not None else "idle"
    run_id = raw_pbi.get("run_id")
    last_error = raw_pbi.get("last_error")
    result = raw_pbi.get("result")
    active = bool(raw_pbi.get("active"))
    archived = bool(raw_pbi.get("archived"))
    claimable = bool(raw_pbi.get("claimable"))
    autonomous_status: str | None = None
    autonomous_action = next(
        (
            action
            for action in matching_actions
            if action.get("kind") == "autonomous_start"
        ),
        None,
    )
    requeue_action = next(
        (
            action
            for action in matching_actions
            if action.get("kind") == "requeue" and action.get("status") == "succeeded"
        ),
        None,
    )
    provider_completed = _provider_completion_confirmed(
        planning_status, metadata.get("issue_state"), pull_requests
    )
    if autonomous_action is not None and provider_completed:
        status = "completed"
        run_id = None
        raw_stage = "merge"
        active = False
        archived = True
        claimable = False
        autonomous_status = "completed"
        last_error = None
    elif autonomous_action is not None:
        autonomous_result = mapping(autonomous_action.get("result"))
        if autonomous_action.get("status") == "pending":
            status = "active"
            run_id = autonomous_action.get("run_id")
            active = True
            claimable = False
            autonomous_status = "running"
        elif (
            autonomous_action.get("status") == "succeeded"
            and autonomous_result.get("status") == "completed"
        ):
            autonomous_status = "completed"
            run_id = None
            claimable = False
            result = autonomous_result.get("summary") or result
        elif autonomous_action.get("status") == "failed":
            status = "failed"
            run_id = autonomous_action.get("run_id")
            active = True
            claimable = False
            autonomous_status = "blocked"
            last_error = (
                autonomous_action.get("error")
                or autonomous_result.get("error")
                or last_error
            )
    requeue_index = next(
        (
            index
            for index, action in enumerate(matching_actions)
            if action is requeue_action
        ),
        None,
    )
    autonomous_index = next(
        (
            index
            for index, action in enumerate(matching_actions)
            if action is autonomous_action
        ),
        None,
    )
    requeue_is_latest = requeue_index is not None and (
        autonomous_index is None or requeue_index < autonomous_index
    )
    if requeue_is_latest and not provider_completed:
        status = "idle"
        run_id = None
        active = bool(raw_pbi.get("active"))
        archived = False
        claimable = True
        autonomous_status = None
        last_error = None
        result = None
    autonomous_handoffs: list[dict[str, object]] = []
    for action in reversed(matching_actions):
        if not str(action.get("kind", "")).startswith("skill:"):
            continue
        handoff_result = action.get("result")
        if isinstance(handoff_result, Mapping):
            autonomous_handoffs.append(dict(cast(Mapping[str, object], handoff_result)))
    display_stage_value = display_stage(
        raw_stage, status, planning_status, pull_requests
    )
    queue_actions = matching_actions
    if status in {"active", "awaiting_operator"} and isinstance(run_id, str):
        queue_actions = [
            action for action in matching_actions if action.get("run_id") == run_id
        ]
    if requeue_is_latest:
        queue_requeue_index = next(
            (
                index
                for index, action in enumerate(queue_actions)
                if action.get("kind") == "requeue"
                and action.get("status") == "succeeded"
            ),
            None,
        )
        if queue_requeue_index is not None:
            queue_actions = queue_actions[: queue_requeue_index + 1]
    workflow_queue = queue_for_pbi(
        status,
        planning_status,
        raw_stage,
        archived,
        raw_pbi.get("branch"),
        pull_requests,
        checks,
        queue_actions,
        canonical_lifecycle,
    )
    return {
        "id": raw_pbi.get("id"),
        "number": raw_pbi.get("number"),
        "title": raw_pbi.get("title"),
        "stage": raw_stage,
        "stage_label": display_stage_label(display_stage_value, planning_status),
        "stage_progress": stage_progress(display_stage_value, planning_status),
        "status": status,
        "attempt": raw_pbi.get("attempt"),
        "run_id": run_id,
        "branch": raw_pbi.get("branch"),
        "pull_request_url": raw_pbi.get("pull_request_url"),
        "last_error": last_error,
        "result": result,
        "task_contract": mapping(raw_pbi.get("task_contract")) or None,
        "task_result": mapping(raw_pbi.get("task_result")) or None,
        "task_answer": raw_pbi.get("task_answer"),
        "canonical_lifecycle": canonical_lifecycle,
        "operator_questions": sequence(raw_pbi.get("operator_questions")),
        "agent_session": mapping(raw_pbi.get("agent_session")) or None,
        "graph_trace": mappings(raw_pbi.get("graph_trace")),
        "active": active,
        "archived": archived,
        "planning_status": planning_status,
        "issue_state": metadata.get("issue_state"),
        "state_reason": metadata.get("state_reason"),
        "source_url": source_url,
        "claimable": claimable,
        "checks": checks,
        "subtasks": subtasks,
        "dependency_readiness": dependency_readiness,
        "pull_requests": pull_requests,
        "readers": readers,
        "reviewers": reviewers,
        "escalation": escalation[0],
        "escalation_log": escalation[1],
        "activity": [*(sequence(metadata.get("activity"))), *events],
        "events": events,
        "operator_actions": matching_actions,
        "autonomous_handoffs": autonomous_handoffs,
        "autonomous_status": autonomous_status,
        "workflow_queue": workflow_queue,
    }


def latest_metadata(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for event in events:
        details = mapping(event.get("details"))
        for key in (
            "readers",
            "reviewers",
            "subtasks",
            "dependency_readiness",
            "checks",
            "escalation",
            "escalation_log",
        ):
            if key in details:
                metadata[key] = details[key]
    return metadata


def escalation_view(
    metadata: Mapping[str, object], events: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], list[object]]:
    raw_escalation = mapping(metadata.get("escalation"))
    consecutive = integer(raw_escalation.get("consecutive"))
    current = integer(raw_escalation.get("current"))
    current_tier = raw_escalation.get("current_tier")
    escalation: dict[str, object] = {
        "current": current,
        "consecutive": consecutive,
    }
    if isinstance(current_tier, str) and current_tier.strip():
        escalation["current_tier"] = current_tier
    raw_log: list[object] = list(sequence(metadata.get("escalation_log")))
    if not raw_log:
        raw_log = [
            mapping(event.get("details"))
            for event in events
            if event.get("type") in {"escalation", "triage"}
        ]
    return escalation, raw_log


def action_matches(
    action: Mapping[str, object],
    repository_name: object,
    raw_pbi: Mapping[str, object],
) -> bool:
    action_repository = action.get("repository")
    action_number = action.get("pbi_number")
    return action_repository == repository_name and action_number == raw_pbi.get(
        "number"
    )
