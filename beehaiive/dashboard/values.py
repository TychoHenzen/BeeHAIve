from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast


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
