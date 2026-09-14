from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .handoff_request import HandoffRequest

__all__ = ["HandoffMutationAudit"]


class HandoffMutationAudit(Protocol):
    """Persistence boundary for external handoff mutations."""

    def begin_handoff_mutation(
        self,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        target: Mapping[str, object],
    ) -> str:
        """Persist a redacted attempt before sending it to GitHub."""

        ...

    def finish_handoff_mutation(
        self,
        action_id: str,
        status: str,
        result: Mapping[str, object],
    ) -> None:
        """Persist the attempt outcome without provider request data."""

        ...

    def reconcile_handoff_mutation(
        self,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        status: str,
        result: Mapping[str, object],
    ) -> None:
        """Resolve unfinished attempts after reading the provider state."""

        ...

    def handoff_mutation_action(
        self, request: HandoffRequest, mutation: str, operation_key: str
    ) -> dict[str, object] | None:
        """Return the latest durable record for one operation."""

        ...
