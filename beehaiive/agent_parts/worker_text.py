from __future__ import annotations

import os

from beehaiive.workflow import GateResult

from .constants import (
    _BEARER_TOKEN,
    _SAFE_ENVIRONMENT_NAMES,
    _SECRET_ASSIGNMENT,
    _SECRET_JSON,
    _SECRET_NAME,
    _URL_CREDENTIALS,
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
        redacted = redacted.replace(secret, "[redacted]")
    redacted = _SECRET_JSON.sub(r"\1[redacted]", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]@", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]", redacted
    )
    return redacted if max_length is None else redacted[:max_length]


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
    "safe_worker_environment",
    "worker_secret_values",
    "_gate_summary",
]
