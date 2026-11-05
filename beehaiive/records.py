"""Read Claude Code transcript files incrementally and pick fields out safely.

Claude Code's transcript format is internal and undocumented. Everything here
treats a record as untrusted JSON: a missing or oddly typed field yields None,
never an exception, so a format change degrades the view instead of breaking it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import cast

Record = dict[str, object]


def as_dict(value: object) -> Record | None:
    return cast(Record, value) if isinstance(value, dict) else None


def as_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def dig(value: object, *keys: str) -> object:
    """Follow nested object keys, returning None at the first missing step."""
    for key in keys:
        record = as_dict(value)
        if record is None:
            return None
        value = record.get(key)
    return value


def get_str(record: Record | None, key: str) -> str | None:
    if record is None:
        return None
    value = record.get(key)
    return value if isinstance(value, str) else None


def get_int(record: Record | None, key: str) -> int:
    if record is None:
        return 0
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def text_of(content: object) -> str:
    """Flatten message content (a string or a list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in as_list(content):
        block = as_dict(item)
        text = get_str(block, "text") or get_str(block, "content")
        if text:
            parts.append(text)
    return "\n".join(parts)


def read_json_file(path: Path) -> Record | None:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def type_markers(*types: str) -> tuple[bytes, ...]:
    """Byte patterns that any line of one of these record types must contain."""
    return tuple(
        marker
        for name in types
        for marker in (f'"type":"{name}"'.encode(), f'"type": "{name}"'.encode())
    )


class TranscriptTail:
    """Return only the complete lines appended to a JSONL file since last read.

    With markers set, a line that contains none of them is skipped unparsed.
    A false match only costs a parse, so the check is safe; it exists because
    most of a transcript's bytes are record types the dashboard never uses.
    """

    def __init__(self, path: Path, markers: tuple[bytes, ...] = ()) -> None:
        self.path = path
        self.markers = markers
        self.offset = 0
        self.unreadable = 0
        self.was_reset = False
        self._partial = b""

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return self.offset

    def read_new(self) -> list[Record]:
        self.was_reset = False
        size = self.size()
        if size < self.offset:
            # The file was replaced or truncated; start over from the top.
            self.offset = 0
            self._partial = b""
            self.unreadable = 0
            self.was_reset = True
        if size == self.offset:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
        except OSError:
            return []
        self.offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        records: list[Record] = []
        for line in lines:
            if not line.strip():
                continue
            if self.markers and not any(m in line for m in self.markers):
                continue
            try:
                record = as_dict(json.loads(line))
            except ValueError:
                record = None
            if record is None:
                self.unreadable += 1
            else:
                records.append(record)
        return records
