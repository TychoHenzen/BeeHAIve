from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from typing import Any, cast

from beehaiive.github.constants import (
    SECONDARY_RATE_LIMIT_FALLBACK_SECONDS as SECONDARY_RATE_LIMIT_FALLBACK_SECONDS,
)
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)


def _header_value(response: object, name: str) -> str | None:
    """Read a response header from urllib responses and test doubles."""

    wanted = name.lower()
    headers: object = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for key, value in cast(Mapping[object, object], headers).items():
            if str(key).lower() == wanted:
                return str(value)
    elif headers is not None:
        items = getattr(cast(Any, headers), "items", None)
        if callable(items):
            header_items = cast(Iterable[tuple[object, object]], items())
            for key, value in header_items:
                if str(key).lower() == wanted:
                    return str(value)

    return None


def _header_float(response: object, name: str) -> float | None:
    value = _header_value(response, name)
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _rate_limit_wait_seconds(
    error: GitHubRateLimitError,
    *,
    previous_secondary_wait: float | None,
) -> float:
    now = time.time()
    reset_at = (
        error.reset_at
        if error.reset_at is not None and math.isfinite(error.reset_at)
        else None
    )
    retry_after = (
        error.retry_after
        if error.retry_after is not None and math.isfinite(error.retry_after)
        else None
    )
    if error.primary:
        if reset_at is not None and reset_at > now:
            return max(0.0, reset_at - now)
        if retry_after is not None:
            return max(0.0, retry_after)
        return SECONDARY_RATE_LIMIT_FALLBACK_SECONDS

    if retry_after is not None:
        delay = max(0.0, retry_after)
    elif (
        error.remaining is not None
        and error.remaining <= 0
        and reset_at is not None
        and reset_at > now
    ):
        delay = max(0.0, reset_at - now)
    else:
        delay = SECONDARY_RATE_LIMIT_FALLBACK_SECONDS
    if previous_secondary_wait is not None:
        delay = max(delay, previous_secondary_wait * 2)
        if previous_secondary_wait == 0:
            delay = max(delay, SECONDARY_RATE_LIMIT_FALLBACK_SECONDS)
    return delay


def _rate_error_details(errors: object) -> tuple[str, bool] | None:
    if not isinstance(errors, list):
        return None
    for raw_error in cast(list[object], errors):
        if not isinstance(raw_error, Mapping):
            continue
        error = cast(Mapping[str, object], raw_error)
        error_type = str(error.get("type", "")).upper()
        code = str(error.get("code", "")).lower()
        message = str(error.get("message", ""))
        normalized_message = message.lower()
        if not (
            error_type in {"RATE_LIMIT", "RATE_LIMITED"}
            or code in {"graphql_rate_limit", "rate_limit", "rate_limited"}
            or "rate limit" in normalized_message
        ):
            continue
        primary = code == "graphql_rate_limit" or error_type == "RATE_LIMIT"
        return message or "GitHub GraphQL rate limit exceeded", primary
    return None


__all__ = [
    "_header_value",
    "_header_float",
    "_rate_limit_wait_seconds",
    "_rate_error_details",
]
