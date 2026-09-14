from __future__ import annotations

from .pbi_creation_error import PbiCreationError

__all__ = ["PbiCreationValidationError"]


class PbiCreationValidationError(PbiCreationError):
    """Raised when the request or its GitHub targets fail validation."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message, code=code, status_code=422)
