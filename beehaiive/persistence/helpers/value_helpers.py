from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from beehaiive.models import (
    ARCHIVE_PROJECT_STATUS,
    PROJECT_TERMINAL_STATUSES,
    PbiSnapshot,
    RunStatus,
)

from ..constants import _REFINEMENT_URL as _REFINEMENT_URL
from ..constants import _SENSITIVE_URL_PARTS as _SENSITIVE_URL_PARTS
from ..constants import (
    MAX_META_REVIEW_EVENT_DETAILS_LENGTH as MAX_META_REVIEW_EVENT_DETAILS_LENGTH,
)


def _json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}


def _json_mapping_or_none(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], decoded) if isinstance(decoded, dict) else None


def _archive_eligible(pbi: PbiSnapshot, run_status: str | None = None) -> bool:
    if run_status in {
        RunStatus.ACTIVE.value,
        RunStatus.AWAITING_OPERATOR.value,
        RunStatus.FAILED.value,
    }:
        return False
    if (pbi.planning_status or "").strip().lower() not in PROJECT_TERMINAL_STATUSES:
        return False
    if (pbi.planning_status or "").strip().lower() != ARCHIVE_PROJECT_STATUS:
        return False
    pull_requests = pbi.metadata.get("pull_requests")
    if not isinstance(pull_requests, Sequence) or isinstance(
        pull_requests, (str, bytes, bytearray)
    ):
        return False
    return any(
        isinstance(raw_pull_request, Mapping)
        and (pull_request := cast(Mapping[str, object], raw_pull_request)).get("merged")
        is True
        and pull_request.get("source_branch_state") == "deleted"
        for raw_pull_request in cast(Sequence[object], pull_requests)
    )


def _bounded_event_details(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value) > MAX_META_REVIEW_EVENT_DETAILS_LENGTH:
        return {}
    return _json_mapping(value)


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return cast(list[object], decoded) if isinstance(decoded, list) else []


def _contains_signed_url(value: str) -> bool:
    for match in _REFINEMENT_URL.finditer(value):
        url = match.group().rstrip(".,);]")
        try:
            query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        except ValueError:
            continue
        for name, _ in query:
            parts = set(re.split(r"[_\-.]+|(?<=[a-z0-9])(?=[A-Z])", name))
            if bool({part.lower() for part in parts} & _SENSITIVE_URL_PARTS):
                return True
    return False


__all__ = [
    "_json_mapping",
    "_json_mapping_or_none",
    "_archive_eligible",
    "_bounded_event_details",
    "_json_list",
    "_contains_signed_url",
]
