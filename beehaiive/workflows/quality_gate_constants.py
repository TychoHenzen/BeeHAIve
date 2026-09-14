from __future__ import annotations

import re

MANIFEST_NAME = "beehaiive-gates.json"


MAX_MANIFEST_BYTES = 256_000


MAX_GATE_COUNT = 32


MAX_GATE_TIMEOUT_SECONDS = 900.0


GATE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


CATEGORY = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


ROOT_KEYS = {"version", "gates"}


GATE_KEYS = {
    "name",
    "argv",
    "timeout_seconds",
    "category",
    "required",
    "external_only",
}


WINDOWS_RUNNER_ERROR_PREFIX = "\x00BEEHAIIVE_GATE_RUNNER_ERROR:"


WINDOWS_GATE_RUNNER = f"""
import json, subprocess, sys
spec = json.loads(sys.stdin.buffer.read())
try:
    result = subprocess.run(spec["argv"], stdin=subprocess.DEVNULL, check=False)
except (OSError, ValueError) as exc:
    sys.stderr.write({WINDOWS_RUNNER_ERROR_PREFIX!r} + type(exc).__name__ + "\\x00")
    raise SystemExit(127)
raise SystemExit(result.returncode)
"""


JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


PROCESS_TERMINATE = 0x0001


PROCESS_SET_QUOTA = 0x0100
