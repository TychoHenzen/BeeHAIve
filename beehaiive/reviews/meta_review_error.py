from __future__ import annotations

from ..storage import (
    StoreError,
)


class MetaReviewError(StoreError):
    """Raised when a bounded meta-review cannot start or be reviewed."""
