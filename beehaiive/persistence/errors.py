from __future__ import annotations


class StoreError(RuntimeError):
    """Raised when persisted orchestration state cannot satisfy an operation."""


class StateConflictError(StoreError):
    """Raised when persisted state no longer matches an expected state."""


__all__ = ["StateConflictError", "StoreError"]
