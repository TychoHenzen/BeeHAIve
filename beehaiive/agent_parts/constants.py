from __future__ import annotations

import re

DEMO_TASK_NAME = "bounded repository inventory"

DEFAULT_DEMO_TASK = (
    "Inspect only the current checkout and report the repository name, current "
    "branch, and count of tracked files. The runner supplies verified Git "
    "metadata because the isolated checkout omits .git. Confirm the copied "
    "files are present, then report that metadata. Do not edit files, create "
    "files, access the network, read credentials, or start other agents. Return "
    "a concise plain-text result."
)

MAX_AGENT_OUTPUT_LENGTH = 4_000

MAX_AGENT_OUTPUT_BYTES = 64_000

MAX_AGENT_TIMEOUT_SECONDS = 900.0

POST_TERMINATION_GRACE_SECONDS = 1.0

_SECRET_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)",
    re.IGNORECASE,
)

_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "COLORTERM",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATHEXT",
        "PATH",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TERM_PROGRAM",
        "TMP",
        "USER",
        "USERPROFILE",
        "USERNAME",
        "WINDIR",
    }
)

_ROUTING_MODEL_ALIASES = frozenset({"luna", "terra", "sol", "astra", "human"})

_SESSION_PROGRESS_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
    }
)

_SECRET_ASSIGNMENT = re.compile(
    r"\b((?:[\w-]+[_-])?(?:access[_-]?token|refresh[_-]?token|token|"
    r"api[_-]?key|secret|password|client[_-]?secret|private[_-]?key|"
    r"credential))\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)

_SECRET_JSON = re.compile(
    r"([\"']?(?:(?:[\w-]+[_-])?(?:access[_-]?token|refresh[_-]?token|"
    r"token|api[_-]?key|client[_-]?secret|private[_-]?key|credential|"
    r"secret|password))[\"']?\s*:\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\[[^\]]*\]|"
    r"\{[^}]*\}|[^,}\s]+)",
    re.IGNORECASE,
)

_BEARER_TOKEN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)

_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+:[^@\s]+@", re.IGNORECASE)

__all__ = [
    "DEFAULT_DEMO_TASK",
    "DEMO_TASK_NAME",
    "MAX_AGENT_OUTPUT_BYTES",
    "MAX_AGENT_OUTPUT_LENGTH",
    "MAX_AGENT_TIMEOUT_SECONDS",
    "POST_TERMINATION_GRACE_SECONDS",
    "_BEARER_TOKEN",
    "_ROUTING_MODEL_ALIASES",
    "_SAFE_ENVIRONMENT_NAMES",
    "_SECRET_ASSIGNMENT",
    "_SECRET_JSON",
    "_SECRET_NAME",
    "_SESSION_PROGRESS_EVENTS",
    "_URL_CREDENTIALS",
]
