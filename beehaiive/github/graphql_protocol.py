from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class GraphQLClient(Protocol):
    """Small client boundary that can be replaced by a GitHub API test double."""

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        """Execute a GraphQL operation and return its data object."""

        ...


__all__ = ["GraphQLClient"]
