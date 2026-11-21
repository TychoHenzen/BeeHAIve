"""Read how an agent's work ended from the last thing it said."""

from __future__ import annotations

import re

_SKILL_LOADED = re.compile(r"^\s*Skill loaded:\s*\S+(?:\s*\([^)]*\))?\s*[.:]?\s*", re.I)
_MARKUP = re.compile(r"\*{1,3}|`")
# ASSUMPTION: dod-guard skills report an early stop as "Stop reason: <why>".
_STOP = re.compile(r"\bstop reason\b\s*[:\-]?\s*(.+)", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_HASH_NUMBER = re.compile(r"#(\d{1,7})\b")
# A bare number that is part of a date, a time or a version is not an issue.
_BARE_NUMBER = re.compile(r"(?<![\d./:-])\b(\d{2,7})\b(?![./:-]\d)")
_NO_STOP = frozenset({"", "none", "n/a", "na", "null", "no", "-", "nothing"})
_FILLER = frozenset({"for", "pr", "issue", "of", "on", "the", "to"})


def clean(text: str) -> str:
    """Drop the skill banner and markdown emphasis, keep the words."""
    text = _SKILL_LOADED.sub("", text)
    return " ".join(_MARKUP.sub("", text).split())


def first_sentence(text: str, limit: int = 160) -> str:
    text = clean(text)
    match = _SENTENCE_END.search(text)
    sentence = text[: match.start()] if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 3] + "..."


def stop_reason(text: str) -> str | None:
    match = _STOP.search(clean(text))
    if not match:
        return None
    reason = first_sentence(match.group(1)).rstrip(".")
    # "Stop reason: none" reports that the stage did not stop.
    return None if reason.strip().lower() in _NO_STOP else reason


def issue_in(text: str) -> int | None:
    """The issue or pull request number a dispatch names, if any."""
    match = _HASH_NUMBER.search(text) or _BARE_NUMBER.search(text)
    return int(match.group(1)) if match else None


def stage_name(description: str, issue: int | None) -> str:
    """Name a dispatch that has no "Skill:" line from its description."""
    numbers = {str(issue), f"#{issue}"} if issue is not None else set[str]()
    words = [
        word
        for word in description.split()
        if word not in numbers and word.lower() not in _FILLER
    ]
    return " ".join(words[:3]) or description
