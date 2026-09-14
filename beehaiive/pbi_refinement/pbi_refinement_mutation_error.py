from __future__ import annotations

__all__ = ["PbiRefinementMutationError"]


class PbiRefinementMutationError(ValueError):
    """A bounded request or live-state conflict for a PBI refinement write."""

    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
