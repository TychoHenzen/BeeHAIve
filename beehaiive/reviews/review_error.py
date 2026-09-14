from __future__ import annotations


class ReviewError(RuntimeError):
    """Raised when a review cycle cannot accept a state transition."""
