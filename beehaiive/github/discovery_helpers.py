from __future__ import annotations

import math
import os

from beehaiive.github.constants import (
    DEFAULT_DISCOVERY_CACHE_SECONDS as DEFAULT_DISCOVERY_CACHE_SECONDS,
)
from beehaiive.github.constants import (
    DISCOVERY_CACHE_SECONDS_ENV as DISCOVERY_CACHE_SECONDS_ENV,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError


def _discovery_cache_seconds_from_environment() -> float:
    raw_value = os.environ.get(
        DISCOVERY_CACHE_SECONDS_ENV, str(DEFAULT_DISCOVERY_CACHE_SECONDS)
    )
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ProviderError(
            f"{DISCOVERY_CACHE_SECONDS_ENV} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise ProviderError(
            f"{DISCOVERY_CACHE_SECONDS_ENV} must be a finite non-negative number"
        )
    return value


__all__ = ["_discovery_cache_seconds_from_environment"]
