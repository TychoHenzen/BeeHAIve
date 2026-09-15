from __future__ import annotations

import re

MAX_CONTRACT_ITEMS = 20
MAX_CONTRACT_TEXT = 1_000
MAX_RESULT_TEXT = 4_000
MAX_RESULT_KEYS = 40
MAX_RESULT_DEPTH = 4
_SECRET_JSON = re.compile(
    r"([\"']?(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|"
    r"client[_-]?secret|private[_-]?key|credential|secret|password)"
    r"[\"']?\s*:\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"\[redacted\]|\[[\s\S]*\]|\{[\s\S]*\}|[^,}\s]+)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"private[_-]?key|credential|token|api[_-]?key|secret|password)\b"
    r"\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"\[(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\"'\]])*\]|"
    r"\{[\s\S]*\}|"
    r"[^\s,;\x7b\x7d\x5b]+(?:\s+(?!(?:[\"']?[A-Za-z_][A-Za-z0-9_-]*"
    r"[\"']?\s*[:=])|[\x7b\x5b])[^\s,;\x7b\x7d\x5b]+)*)",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+:[^@\s]+@", re.IGNORECASE)

__all__ = [
    "MAX_CONTRACT_ITEMS",
    "MAX_CONTRACT_TEXT",
    "MAX_RESULT_DEPTH",
    "MAX_RESULT_KEYS",
    "MAX_RESULT_TEXT",
    "_BEARER_TOKEN",
    "_SECRET_ASSIGNMENT",
    "_SECRET_JSON",
    "_URL_CREDENTIALS",
]
