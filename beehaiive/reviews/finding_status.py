from __future__ import annotations

from enum import StrEnum


class FindingStatus(StrEnum):
    """Resolution state of a reader finding."""

    OPEN = "open"
    RESOLVED = "resolved"
