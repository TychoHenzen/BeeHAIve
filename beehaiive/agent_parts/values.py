from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast


def _nonnegative_int(value: object, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _text_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for raw_item in cast(list[object], value):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, object], raw_item)
            if isinstance(item.get("text"), str):
                parts.append(_text_value(item.get("text")))
        return "\n".join(part for part in parts if part).strip()
    return ""


def resolve_executable(executable: str) -> str:
    configured = executable.strip()
    if Path(configured).suffix.lower() in {".bat", ".cmd", ".exe"}:
        return shutil.which(configured) or configured
    return shutil.which(f"{configured}.exe") or shutil.which(configured) or configured


__all__ = ["_nonnegative_int", "_text_value", "resolve_executable"]
