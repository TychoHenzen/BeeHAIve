from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from .constants import (
    DEFAULT_SCHEDULER_MAX_CONCURRENCY,
    DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS,
    SCHEDULER_ENABLED_ENV,
    SCHEDULER_MAX_CONCURRENCY_ENV,
    SCHEDULER_POLL_INTERVAL_ENV,
)

__all__ = ["SchedulerConfig"]


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    enabled: bool = False
    poll_interval_seconds: float = DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS
    max_concurrency: int = DEFAULT_SCHEDULER_MAX_CONCURRENCY

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError(f"{SCHEDULER_ENABLED_ENV} must be true or false")
        if (
            type(self.poll_interval_seconds) not in {int, float}
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError(
                f"{SCHEDULER_POLL_INTERVAL_ENV} must be a finite positive number"
            )
        if type(self.max_concurrency) is not int or self.max_concurrency <= 0:
            raise ValueError(
                f"{SCHEDULER_MAX_CONCURRENCY_ENV} must be a positive integer"
            )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> SchedulerConfig:
        values = os.environ if environ is None else environ
        raw_enabled = values.get(SCHEDULER_ENABLED_ENV, "false").strip().lower()
        if raw_enabled in {"true", "1", "yes", "on"}:
            enabled = True
        elif raw_enabled in {"false", "0", "no", "off"}:
            enabled = False
        else:
            raise ValueError(f"{SCHEDULER_ENABLED_ENV} must be true or false")

        raw_interval = values.get(
            SCHEDULER_POLL_INTERVAL_ENV,
            str(DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS),
        )
        try:
            poll_interval_seconds = float(raw_interval)
        except ValueError as exc:
            raise ValueError(
                f"{SCHEDULER_POLL_INTERVAL_ENV} must be a finite positive number"
            ) from exc

        raw_concurrency = values.get(
            SCHEDULER_MAX_CONCURRENCY_ENV,
            str(DEFAULT_SCHEDULER_MAX_CONCURRENCY),
        )
        try:
            max_concurrency = int(raw_concurrency)
        except ValueError as exc:
            raise ValueError(
                f"{SCHEDULER_MAX_CONCURRENCY_ENV} must be a positive integer"
            ) from exc

        return cls(enabled, poll_interval_seconds, max_concurrency)
