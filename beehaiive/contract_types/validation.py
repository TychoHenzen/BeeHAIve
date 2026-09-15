from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import cast

from .constants import (
    _BEARER_TOKEN,
    _SECRET_ASSIGNMENT,
    _SECRET_JSON,
    _URL_CREDENTIALS,
    MAX_CONTRACT_ITEMS,
    MAX_CONTRACT_TEXT,
    MAX_RESULT_DEPTH,
    MAX_RESULT_KEYS,
    MAX_RESULT_TEXT,
)
from .contract_error import ContractError


def _redact_text(value: str, limit: int = MAX_RESULT_TEXT) -> str:
    variants = {value}
    normalized = value
    for _ in range(2):
        normalized = normalized.replace(r"\"", '"').replace(r"\'", "'")
        variants.add(normalized)
    candidates: list[str] = []
    for variant in variants:
        candidate = variant
        for _ in range(3):
            updated = _redact_once(candidate)
            if updated == candidate:
                break
            candidate = updated
        candidates.append(candidate)
    redacted = min(
        candidates,
        key=lambda candidate: (-candidate.count("[redacted]"), len(candidate)),
    )
    return redacted.strip()[:limit]


def _redact_once(value: str) -> str:
    redacted = _SECRET_JSON.sub(r"\1[redacted]", value)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]@", redacted)
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]", redacted
    )


def _bounded_value(value: object, depth: int = 0) -> object:
    if depth > MAX_RESULT_DEPTH:
        raise ContractError("Result data is nested too deeply")
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("Result data contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if len(mapping) > MAX_RESULT_KEYS:
            raise ContractError("Result data contains too many keys")
        bounded: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ContractError("Result data keys must be non-empty strings")
            bounded[key[:MAX_CONTRACT_TEXT]] = _bounded_value(item, depth + 1)
        return bounded
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        if len(sequence) > MAX_CONTRACT_ITEMS:
            raise ContractError("Result data contains too many items")
        return [_bounded_value(item, depth + 1) for item in sequence]
    raise ContractError("Result data must contain JSON-compatible values")


def _text(value: object, field_name: str, *, required: bool = True) -> str | None:
    if not isinstance(value, str):
        if required:
            raise ContractError(f"{field_name} must be a string")
        return None
    normalized = _redact_text(value, MAX_CONTRACT_TEXT)
    if required and not normalized:
        raise ContractError(f"{field_name} is required")
    return normalized or None


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{field_name} must be a list")
    values: list[str] = []
    for item in cast(Sequence[object], value):
        text = _text(item, field_name)
        values.append(text or "")
    return tuple(values)


def _validate_unique_strings(values: Sequence[str], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ContractError(f"{field_name} must contain non-empty strings")
    if len(set(values)) != len(values):
        raise ContractError(f"{field_name} must be unique")


__all__ = [
    "_redact_text",
    "_bounded_value",
    "_text",
    "_string_tuple",
    "_validate_unique_strings",
]
