from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    VALIDATION = "validation"
    TARGET_PROVIDER = "target_provider"
    WORLD = "world"
    MODEL = "model"
    PERSISTENCE = "persistence"
    UNEXPECTED = "unexpected"


class TargetProviderError(RuntimeError):
    """An expected failure while reading authoritative target metadata."""


class WorldActionError(RuntimeError):
    """An expected failure while reading or applying world state."""


__all__ = [
    "FailureCategory",
    "TargetProviderError",
    "WorldActionError",
]
