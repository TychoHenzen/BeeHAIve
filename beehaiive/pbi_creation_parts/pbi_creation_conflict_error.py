from __future__ import annotations

from .pbi_creation_error import PbiCreationError

__all__ = ["PbiCreationConflictError"]


class PbiCreationConflictError(PbiCreationError):
    """Raised when a key is reused with a different request."""

    def __init__(self) -> None:
        super().__init__(
            "Idempotency-Key conflicts with an earlier request",
            code="idempotency_key_conflict",
            status_code=409,
        )
