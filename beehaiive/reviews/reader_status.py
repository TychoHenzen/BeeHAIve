from __future__ import annotations

from enum import StrEnum


class ReaderStatus(StrEnum):
    """State returned by one specialized reader."""

    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
