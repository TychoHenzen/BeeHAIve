from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from .check_result import CheckResult
from .handoff_status import HandoffStatus
from .workflow_error import WorkflowError
from .workflow_role import WorkflowRole


def current_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def require_text(value: str, label: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise WorkflowError(f"{label} is required")
    if len(normalized) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return normalized


def require_path(value: str, label: str, limit: int = 1_000) -> str:
    if not value.strip():
        raise WorkflowError(f"{label} is required")
    if len(value) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return value


def optional_text(value: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > limit:
        raise WorkflowError(f"Value must be at most {limit} characters")
    return normalized


def lease_expiry(ttl_seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()


def lease_is_expired(expires_at: object) -> bool:
    if not isinstance(expires_at, str) or not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except (TypeError, ValueError):
        return True


def repository_identity(remote: str) -> str | None:
    normalized = remote.replace("\\", "/")
    match = re.search(r"([^/:\s]+/[^/\s]+?)(?:\.git)?$", normalized)
    return None if match is None else match.group(1)


def enum_role(value: object) -> WorkflowRole:
    try:
        return WorkflowRole(value)
    except ValueError as exc:
        raise WorkflowError(f"Unknown workflow role: {value}") from exc


def enum_status(value: object) -> HandoffStatus:
    try:
        return HandoffStatus(value)
    except ValueError as exc:
        raise WorkflowError(f"Unknown handoff status: {value}") from exc


def checks_to_json(checks: Sequence[CheckResult]) -> str:
    return json.dumps([check.as_dict() for check in checks], sort_keys=True)


def checks_from_json(value: object) -> tuple[CheckResult, ...]:
    if not isinstance(value, str):
        raise WorkflowError("Stored verification evidence is invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowError("Stored verification evidence is invalid") from exc
    if not isinstance(decoded, list):
        raise WorkflowError("Stored verification evidence is invalid")
    checks: list[CheckResult] = []
    for item in cast(list[object], decoded):
        if not isinstance(item, dict):
            raise WorkflowError("Stored verification evidence is invalid")
        mapping = cast(dict[str, object], item)
        name = mapping.get("name")
        passed = mapping.get("passed")
        evidence = mapping.get("evidence")
        status = mapping.get("status", "passed" if passed is True else "failed")
        category = mapping.get("category", "workflow")
        required = mapping.get("required", True)
        argv = mapping.get("argv", [])
        exit_code = mapping.get("exit_code")
        stdout = mapping.get("stdout", "")
        stderr = mapping.get("stderr", "")
        error = mapping.get("error")
        arguments = cast(list[object], argv) if isinstance(argv, list) else []
        if (
            not isinstance(name, str)
            or not isinstance(passed, bool)
            or not isinstance(evidence, str)
            or not isinstance(status, str)
            or status
            not in {
                "passed",
                "failed",
                "timed_out",
                "unavailable",
                "external_only",
                "invalid_configuration",
                "configuration_missing",
            }
            or passed != (status == "passed")
            or not isinstance(category, str)
            or not category
            or type(required) is not bool
            or not isinstance(argv, list)
            or any(not isinstance(argument, str) for argument in arguments)
            or (exit_code is not None and type(exit_code) is not int)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or (error is not None and not isinstance(error, str))
        ):
            raise WorkflowError("Stored verification evidence is invalid")
        checks.append(
            CheckResult(
                name,
                passed,
                evidence,
                status,
                category,
                required,
                tuple(cast(str, argument) for argument in arguments),
                exit_code,
                stdout,
                stderr,
                error,
            )
        )
    return tuple(checks)


def required_checks_pass(checks: Iterable[CheckResult]) -> bool:
    return all(check.passed for check in checks if check.required)
