from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from beehaiive.agent import redact_worker_text, worker_secret_values
from beehaiive.contract_types.validation import is_credential_name

MAX_DASHBOARD_TEXT_LENGTH = 4_000
MAX_DASHBOARD_ITEMS = 100
_SAFE_DASHBOARD_STRING_VALUES = frozenset(
    {
        "action",
        "branch",
        "commit_sha",
        "kind",
        "lease_id",
        "question_id",
        "repository",
        "repository_name",
        "run_id",
        "stage",
        "stage_label",
        "status",
        "target",
    }
)


def mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items = cast(Sequence[object], value)
    return [
        dict(cast(Mapping[str, object], item))
        for item in items
        if isinstance(item, Mapping)
    ]


def sequence(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(cast(Sequence[object], value))


def integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def safe_dashboard_value(
    value: object,
    secret_values: tuple[str, ...] = (),
    depth: int = 0,
    keep_last: bool = False,
    preserve_string: bool = False,
    preserve_items: bool = False,
) -> object:
    secrets = secret_values or worker_secret_values()
    if depth >= 12:
        return "[truncated]"
    if isinstance(value, str):
        if preserve_string:
            return redact_worker_text(value, (), max_length=MAX_DASHBOARD_TEXT_LENGTH)
        return redact_worker_text(value, secrets, max_length=MAX_DASHBOARD_TEXT_LENGTH)
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value).items()
        safe_mapping: dict[str, object] = {}
        for index, (key, item) in enumerate(items):
            if index >= MAX_DASHBOARD_ITEMS:
                break
            raw_key = str(key)
            output_key = raw_key[:200]
            if is_credential_name(raw_key):
                safe_mapping[output_key] = "[redacted]"
            else:
                safe_mapping[output_key] = safe_dashboard_value(
                    item,
                    secrets,
                    depth + 1,
                    raw_key in {"events", "activity", "transition_evidence"},
                    raw_key in _SAFE_DASHBOARD_STRING_VALUES,
                    raw_key
                    in {
                        "supported_actions",
                        "supported_action_owners",
                        "supported_action_readback",
                    },
                )
        return safe_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = cast(Sequence[object], value)
        values = list(items)
        bounded_values = (
            values[-MAX_DASHBOARD_ITEMS:] if keep_last else values[:MAX_DASHBOARD_ITEMS]
        )
        return [
            safe_dashboard_value(
                item,
                secrets,
                depth + 1,
                preserve_string=preserve_items,
            )
            for item in bounded_values
        ]
    return value
