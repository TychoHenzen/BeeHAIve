from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .pbi_creation_progress import PbiCreationProgress
from .pbi_creation_request import PbiCreationRequest
from .pbi_creation_result import PbiCreationResult
from .pbi_creation_target import PbiCreationTarget

__all__ = ["PbiCreationProvider"]


class PbiCreationProvider(Protocol):
    """GitHub operations required to create and reconcile one PBI."""

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        """Validate live project, repository, labels, and Backlog option."""

        ...

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        """Create or reconcile an issue and confirm its Project state."""

        ...
