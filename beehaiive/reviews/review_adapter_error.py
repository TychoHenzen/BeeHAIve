from __future__ import annotations

from .review_error import ReviewError


class ReviewAdapterError(ReviewError):
    """Raised when a provider or reader adapter fails outside review state."""
