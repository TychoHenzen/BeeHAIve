from __future__ import annotations

from enum import StrEnum


class ReviewConcern(StrEnum):
    """Required specialized readers for one review cycle."""

    SECURITY = "security"
    TEST_COVERAGE = "test_coverage"
    CLEAN_CODE = "clean_code"
    PERFORMANCE = "performance"
