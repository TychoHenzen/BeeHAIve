"""Shared types and constants for the dashboard smoke proof."""

from __future__ import annotations

from typing import Final


class SmokeFailure(RuntimeError):
    """Raised when a smoke assertion cannot be proved."""


FIXTURE_PROJECT_ID: Final = "fixture:1"
FIXTURE_API_KEY: Final = "fixture-api-key"
