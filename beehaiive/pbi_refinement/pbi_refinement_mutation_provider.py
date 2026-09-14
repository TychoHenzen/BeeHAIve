from __future__ import annotations

from typing import Protocol

from .pbi_refinement_update_request import PbiRefinementUpdateRequest
from .pbi_refinement_update_result import PbiRefinementUpdateResult

__all__ = ["PbiRefinementMutationProvider"]


class PbiRefinementMutationProvider(Protocol):
    def apply_pbi_refinement(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult: ...
