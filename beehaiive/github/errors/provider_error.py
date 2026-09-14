from __future__ import annotations


class ProviderError(RuntimeError):
    """Raised when a provider cannot discover or hand off work."""


__all__ = ["ProviderError"]
