from __future__ import annotations

__all__ = ["PbiRelationProviderError"]


class PbiRelationProviderError(RuntimeError):
    def __init__(self, code: str = "github_request_failed") -> None:
        super().__init__(code)
        self.code = code
