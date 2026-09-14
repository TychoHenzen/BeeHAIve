from __future__ import annotations

import re

MAX_CONTRACT_ITEMS = 20
MAX_CONTRACT_TEXT = 1_000
MAX_RESULT_TEXT = 4_000
MAX_RESULT_KEYS = 40
MAX_RESULT_DEPTH = 4
_SECRET_JSON = re.compile(
    r"([\"']?(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|"
    r"client[_-]?secret|secret|password)[\"']?\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,}\s]+)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*\S+", re.IGNORECASE
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
