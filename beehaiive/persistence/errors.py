from __future__ import annotations


class StoreError(RuntimeError):
    """Raised when persisted orchestration state cannot satisfy an operation."""


__all__ = ["StoreError"]
