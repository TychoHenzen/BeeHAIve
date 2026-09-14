from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ..pbi_creation import PbiCreationRequest

MAX_META_REVIEW_EVIDENCE_REFS = 25


Analyzer = Callable[[Sequence[Mapping[str, object]]], Sequence[object]]


PbiCreator = Callable[[PbiCreationRequest, str], dict[str, object]]
