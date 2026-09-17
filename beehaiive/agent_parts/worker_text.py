from __future__ import annotations

import os
import re
import traceback

from beehaiive.contract_types.validation import _redact_text as _redact_text
from beehaiive.workflow import GateResult

from .constants import (
    _SAFE_ENVIRONMENT_NAMES,
    _SECRET_NAME,
    MAX_AGENT_OUTPUT_LENGTH,
)


def redact_worker_text(
    text: str,
    secret_values: tuple[str, ...] = (),
    max_length: int | None = MAX_AGENT_OUTPUT_LENGTH,
) -> str:
    """Remove common credential forms before worker text reaches durable state."""

    redacted = text
    for secret in sorted(
        (value for value in secret_values if value), key=len, reverse=True
    ):
        if len(secret) < 8:
            redacted = re.sub(
                rf"(?<![\w]){re.escape(secret)}(?![\w])",
                "[redacted]",
                redacted,
            )
        else:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = _redact_text(
        redacted,
        max_length if max_length is not None else max(len(redacted), 4_000),
    )
    return redacted if max_length is None else redacted[:max_length]


def format_worker_exception(error: Exception) -> str:
    details = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    if isinstance(error, FileNotFoundError):
        details += (
            "\nMissing path details: "
            f"filename={error.filename!r}; "
            f"filename2={getattr(error, 'filename2', None)!r}; "
            f"errno={error.errno}; "
            f"winerror={getattr(error, 'winerror', None)!r}"
        )
    return details.strip()


def safe_worker_environment() -> dict[str, str]:
    """Keep only the environment variables allowed in bounded worker processes."""

    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _SAFE_ENVIRONMENT_NAMES
    }


def worker_secret_values() -> tuple[str, ...]:
    """Return secret-like environment values for redacting worker evidence."""

    return tuple(
        sorted(
            {
                value
                for name, value in os.environ.items()
                if _SECRET_NAME.search(name) and value
            },
            key=len,
            reverse=True,
        )
    )


def _gate_summary(result: GateResult) -> dict[str, object]:
    return {
        "gate": result.gate,
        "allowed": result.allowed,
        "required_action": result.required_action,
        "checks": [
            {
                "name": check.name,
                "status": check.status or ("passed" if check.passed else "failed"),
                "category": check.category,
                "required": check.required,
                "exit_code": check.exit_code,
            }
            for check in result.checks
        ],
    }


__all__ = [
    "redact_worker_text",
    "format_worker_exception",
    "safe_worker_environment",
    "worker_secret_values",
    "_gate_summary",
]
