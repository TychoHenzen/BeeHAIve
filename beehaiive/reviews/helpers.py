from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from .constants import MAX_REVIEW_EVIDENCE_REFS
from .review_error import ReviewError

if TYPE_CHECKING:
    from .review_concern import ReviewConcern


def current_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def claim_is_active(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(UTC)
    except ValueError:
        return False


def require_text(value: str, label: str, limit: int = 200) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ReviewError(f"{label} is required")
    if len(normalized) > limit:
        raise ReviewError(f"{label} must be at most {limit} characters")
    return normalized


def normalized_head_sha(value: str, label: str = "head SHA") -> str:
    normalized = require_text(value, label)
    if normalized != value or any(
        not (character.isalnum() or character in "._/-") for character in normalized
    ):
        raise ReviewError(f"{label} has an invalid format")
    return normalized


def finding_publication_marker(
    pull_request_id: str, head_sha: str, fingerprint: str
) -> str:
    identity = "\0".join(
        (
            require_text(pull_request_id, "pull request id"),
            normalized_head_sha(head_sha),
            require_text(fingerprint, "finding fingerprint", 128),
        )
    )
    marker_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"<!-- beehaiive-finding:v1:{marker_id} -->"


def finding_anchor(
    file_path: str | None, start_line: int | None, end_line: int | None
) -> tuple[str | None, int | None, int | None]:
    if file_path is None:
        if start_line is not None or end_line is not None:
            raise ReviewError("Finding anchor requires a file path and line range")
        return None, None, None
    path = file_path.strip()
    if (
        not path
        or len(path) > 500
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or type(start_line) is not int
        or type(end_line) is not int
        or start_line < 1
        or end_line < start_line
        or end_line > 2_147_483_647
    ):
        raise ReviewError("Finding anchor is invalid")
    return path, start_line, end_line


def finding_fingerprint(
    concern: ReviewConcern,
    summary: str,
    file_path: str | None,
    start_line: int | None,
    end_line: int | None,
) -> str:
    identity = json.dumps(
        {
            "concern": concern.value,
            "summary": summary,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def coerce_enum[T: StrEnum](value: T | str, enum_type: type[T], label: str) -> T:
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ReviewError(f"Unknown {label}: {value}") from exc


def json_list(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewError("Stored reader findings are invalid") from exc
    if not isinstance(decoded, list):
        raise ReviewError("Stored reader findings are invalid")
    items = cast(list[object], decoded)
    if not all(isinstance(item, str) for item in items):
        raise ReviewError("Stored reader findings are invalid")
    return tuple(cast(list[str], items))


def json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewError("Stored GitHub review evidence is invalid") from exc
    if not isinstance(decoded, dict):
        raise ReviewError("Stored GitHub review evidence is invalid")
    return cast(dict[str, object], decoded)


def safe_publication_evidence(
    evidence: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if evidence is None:
        return None
    safe: dict[str, object] = {}
    for key in ("reason", "error_type"):
        value: object = evidence.get(key)
        if isinstance(value, str):
            safe[key] = value[:120]
    remote_ids: object = evidence.get("remote_ids")
    if isinstance(remote_ids, list):
        safe["remote_ids"] = [
            value[:200]
            for value in cast(list[object], remote_ids[:5])
            if isinstance(value, str)
        ]
    retry_after: object = evidence.get("retry_after")
    if type(retry_after) is int and 0 <= retry_after <= 86_400:
        safe["retry_after"] = retry_after
    return safe or None


def github_pull_request_evidence_ref(evidence_json: str | None) -> str | None:
    if evidence_json is None:
        return None
    evidence = json_object(evidence_json)
    pull_request = evidence.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    identifier = cast(dict[str, object], pull_request).get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def normalized_evidence_refs(values: Iterable[str]) -> tuple[str, ...]:
    refs = tuple(
        dict.fromkeys(
            require_text(value, "evidence reference", 500) for value in values
        )
    )
    if len(refs) > MAX_REVIEW_EVIDENCE_REFS:
        raise ReviewError(
            f"A finding can reference at most {MAX_REVIEW_EVIDENCE_REFS} evidence items"
        )
    return refs


def validate_evidence_refs(
    evidence_json: str | None, refs: tuple[str, ...], *, required: bool
) -> None:
    if evidence_json is None:
        if refs:
            raise ReviewError("GitHub evidence references require a provider snapshot")
        return
    if required and not refs:
        raise ReviewError("A GitHub-backed result must reference its evidence")
    source_ids: set[str] = set()

    def collect_ids(value: object) -> None:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[str, object], value)
            identifier = mapping.get("id")
            if isinstance(identifier, str) and identifier:
                source_ids.add(identifier)
            for nested in mapping.values():
                collect_ids(nested)
        elif isinstance(value, list):
            for nested in cast(list[object], value):
                collect_ids(nested)

    collect_ids(json_object(evidence_json))
    if any(ref not in source_ids for ref in refs):
        raise ReviewError("GitHub evidence reference is not in the provider snapshot")
