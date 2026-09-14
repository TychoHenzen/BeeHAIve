from __future__ import annotations

__all__ = ["ContractError"]


class ContractError(ValueError):
    """Raised when a worker contract or result is not safe to use."""
