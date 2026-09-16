from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import cast

from .constants import (
    _BEARER_TOKEN,
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
    redacted = _redact_assignments(value)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]@", redacted)
    return redacted


_CREDENTIAL_MARKERS = (
    "accesstoken",
    "refreshtoken",
    "token",
    "apikey",
    "clientsecret",
    "privatekey",
    "credential",
    "secret",
    "password",
)
_ASSIGNMENT = re.compile(r"(?<![\w-])(?P<key>[\"']?[A-Za-z][\w-]*[\"']?)\s*[:=]")


def is_credential_name(name: str) -> bool:
    """Return whether a field or environment name can carry a credential."""

    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return any(marker in normalized for marker in _CREDENTIAL_MARKERS)


def _consume_quoted(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            return index + 1
        else:
            index += 1
    return len(text)


def _consume_container(text: str, start: int) -> int:
    pairs = {"[": "]", "{": "}"}
    stack = [pairs[text[start]]]
    index = start + 1
    while index < len(text) and stack:
        character = text[index]
        if character in {'"', "'"}:
            index = _consume_quoted(text, index)
            continue
        if character in pairs:
            stack.append(pairs[character])
        elif character == stack[-1]:
            stack.pop()
        index += 1
    return index


def _consume_bare(text: str, start: int, key: str) -> int:
    if "privatekey" in re.sub(r"[^a-z0-9]", "", key.casefold()) or text.startswith(
        "-----BEGIN "
    ):
        end_marker = re.search(r"-----END [^-\r\n]+-----", text[start:], re.IGNORECASE)
        if end_marker is not None:
            return start + end_marker.end()
    next_assignment = _ASSIGNMENT.search(text, start)
    end = len(text)
    if next_assignment is not None:
        end = next_assignment.start()
    for delimiter in ("\r", "\n", ",", ";", "]", "}"):
        delimiter_index = text.find(delimiter, start, end)
        if delimiter_index >= 0:
            end = delimiter_index
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def _consume_value(text: str, start: int, key: str) -> int:
    if start >= len(text):
        return start
    if text[start] in {'"', "'"}:
        return _consume_quoted(text, start)
    if text[start] in {"[", "{"}:
        return _consume_container(text, start)
    return _consume_bare(text, start, key)


def _redact_assignments(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _ASSIGNMENT.finditer(text):
        if match.start() < cursor:
            continue
        raw_key = match.group("key")
        key = raw_key.strip("\"'")
        if not is_credential_name(key):
            continue
        value_start = match.end()
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        value_end = _consume_value(text, value_start, key)
        if value_end <= value_start:
            continue
        replacement = (
            '"[redacted]"'
            if raw_key.startswith('"')
            else "'[redacted]'"
            if raw_key.startswith("'")
            else "[redacted]"
        )
        parts.extend((text[cursor:value_start], replacement))
        cursor = value_end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


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
    "is_credential_name",
    "_bounded_value",
    "_text",
    "_string_tuple",
    "_validate_unique_strings",
]
