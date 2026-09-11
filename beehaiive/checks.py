"""Normalize GitHub pull-request check contexts into bounded evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

CheckVerdict = Literal["pending", "passing", "blocking", "unproven"]

_PENDING_STATES = frozenset(
    {"expected", "in_progress", "pending", "queued", "requested", "waiting"}
)
_PASSING_CONCLUSIONS = frozenset({"neutral", "skipped", "success"})
_BLOCKING_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "error",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalized(value: object) -> str | None:
    text = _text(value)
    return text.lower() if text is not None else None


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _required(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _context_verdict(
    kind: str, status: str | None, state: str | None, conclusion: str | None
) -> CheckVerdict:
    if kind == "check_run":
        if status in _PENDING_STATES:
            return "pending"
        if status != "completed":
            return "unproven"
        if conclusion in _PASSING_CONCLUSIONS:
            return "passing"
        if conclusion in _BLOCKING_CONCLUSIONS:
            return "blocking"
        return "unproven"
    if state in _PENDING_STATES:
        return "pending"
    if state == "success":
        return "passing"
    if state in {"error", "failure"}:
        return "blocking"
    return "unproven"


def _context_record(raw_context: Mapping[str, object]) -> dict[str, object]:
    typename = _text(raw_context.get("__typename"))
    kind = "check_run" if typename == "CheckRun" else "status_context"
    name = (
        _text(raw_context.get("name"))
        if typename == "CheckRun"
        else _text(raw_context.get("context"))
    )
    status = _normalized(raw_context.get("status"))
    state = _normalized(raw_context.get("state"))
    conclusion = _normalized(raw_context.get("conclusion"))
    if typename not in {"CheckRun", "StatusContext"}:
        kind = "unknown"
        name = _text(raw_context.get("name")) or _text(raw_context.get("context"))

    record: dict[str, object] = {
        "kind": kind,
        "name": name or "unknown",
        "required": _required(raw_context.get("isRequired")),
        "verdict": _context_verdict(kind, status, state, conclusion)
        if kind != "unknown"
        else "unproven",
    }
    if status is not None:
        record["status"] = status
    if state is not None:
        record["state"] = state
    if conclusion is not None:
        record["conclusion"] = conclusion

    field_map = {
        "detailsUrl": "url",
        "targetUrl": "url",
        "startedAt": "started_at",
        "completedAt": "completed_at",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
    }
    for source, target in field_map.items():
        value = _text(raw_context.get(source))
        if value is not None:
            record[target] = value
    return record


def _context_nodes(rollup: Mapping[str, object]) -> list[Mapping[str, object]] | None:
    raw_contexts = rollup.get("contexts")
    if not isinstance(raw_contexts, Mapping):
        return None
    contexts = cast(Mapping[str, object], raw_contexts)
    raw_nodes = contexts.get("nodes")
    if not isinstance(raw_nodes, list):
        return None
    nodes = cast(list[object], raw_nodes)
    if any(not isinstance(node, Mapping) for node in nodes):
        return None
    return [cast(Mapping[str, object], node) for node in nodes]


def _verdict(contexts: Sequence[Mapping[str, object]]) -> CheckVerdict:
    if not contexts or any(
        context.get("required") is None or context.get("verdict") == "unproven"
        for context in contexts
    ):
        return "unproven"
    required = [context for context in contexts if context.get("required") is True]
    if any(context.get("verdict") == "blocking" for context in required):
        return "blocking"
    if any(context.get("verdict") == "pending" for context in required):
        return "pending"
    return "passing"


def normalize_check_rollup(
    head_sha: object,
    rollup: object,
    *,
    error: str | None = None,
) -> dict[str, object]:
    """Return a current-head check snapshot without inventing provider state."""

    normalized_head = _text(head_sha)
    snapshot: dict[str, object] = {"head_sha": normalized_head}
    if error is not None:
        snapshot.update({"verdict": "unproven", "contexts": [], "error": error})
        return snapshot

    rollup_mapping = _mapping(rollup)
    commit = _mapping(rollup_mapping.get("commit"))
    rollup_sha = _text(commit.get("oid"))
    snapshot["rollup_sha"] = rollup_sha
    rollup_state = _normalized(rollup_mapping.get("state"))
    if rollup_state is not None:
        snapshot["rollup_state"] = rollup_state
    raw_contexts = _context_nodes(rollup_mapping)
    if (
        normalized_head is None
        or rollup_sha is None
        or normalized_head != rollup_sha
        or raw_contexts is None
    ):
        snapshot.update({"verdict": "unproven", "contexts": []})
        return snapshot

    contexts = [_context_record(raw_context) for raw_context in raw_contexts]
    snapshot["contexts"] = contexts
    snapshot["verdict"] = _verdict(contexts)
    snapshot["blocking_contexts"] = [
        context
        for context in contexts
        if context.get("required") is True and context.get("verdict") == "blocking"
    ]
    return snapshot


def aggregate_check_verdict(checks: Sequence[Mapping[str, object]]) -> CheckVerdict:
    """Combine active pull-request snapshots without hiding an unproven one."""

    if not checks:
        return "unproven"
    verdicts = {check.get("verdict") for check in checks}
    if not verdicts.issubset({"pending", "passing", "blocking", "unproven"}):
        return "unproven"
    if "unproven" in verdicts:
        return "unproven"
    if "blocking" in verdicts:
        return "blocking"
    if "pending" in verdicts:
        return "pending"
    return "passing"


def blocking_check_failure(
    metadata: Mapping[str, object],
) -> str | None:
    """Build one routing-safe error for a required current-head failure."""

    pull_requests = metadata.get("pull_requests")
    if not isinstance(pull_requests, Sequence) or isinstance(
        pull_requests, (str, bytes, bytearray)
    ):
        return None
    for raw_pull_request in cast(Sequence[object], pull_requests):
        pull_request = _mapping(raw_pull_request)
        if _normalized(pull_request.get("state")) != "open":
            continue
        checks = _mapping(pull_request.get("checks"))
        if checks.get("verdict") != "blocking":
            continue
        blocking = checks.get("blocking_contexts")
        if not isinstance(blocking, list) or not blocking:
            continue
        context = _mapping(cast(list[object], blocking)[0])
        number = pull_request.get("number")
        name = _text(context.get("name")) or "unknown"
        state = _text(context.get("state")) or _text(context.get("status")) or "unknown"
        conclusion = _text(context.get("conclusion"))
        head_sha = _text(checks.get("head_sha")) or "unknown"
        url = _text(context.get("url"))
        detail = f"{state}/{conclusion}" if conclusion else state
        message = (
            f"Pull request #{number} check {name!r} blocks head {head_sha}: {detail}"
        )
        return f"{message} ({url})" if url else message
    return None
