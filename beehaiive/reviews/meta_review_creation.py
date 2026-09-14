from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ..models import RunState
from ..pbi_creation import PbiCreationRequest
from ..storage import (
    OrchestratorStore,
)
from .meta_review_error import MetaReviewError
from .meta_review_helpers import source_run_id
from .meta_review_types import MAX_META_REVIEW_EVIDENCE_REFS


def pbi_creation_request(
    store: OrchestratorStore,
    project_id: str,
    suggestion: Mapping[str, object],
) -> PbiCreationRequest:
    suggestion_id = suggestion.get("suggestion_id")
    outcome = suggestion.get("proposed_outcome")
    rationale = suggestion.get("rationale")
    evidence_refs = suggestion.get("evidence_refs")
    if (
        not isinstance(suggestion_id, str)
        or not isinstance(outcome, str)
        or not outcome.strip()
        or not isinstance(rationale, str)
        or not rationale.strip()
        or not isinstance(evidence_refs, list)
    ):
        raise MetaReviewError("Accepted suggestion is incomplete")

    raw_refs = cast(list[object], evidence_refs)
    if not raw_refs or len(raw_refs) > MAX_META_REVIEW_EVIDENCE_REFS:
        raise MetaReviewError("Accepted suggestion is incomplete")
    refs: list[str] = []
    run_ids: list[str] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise MetaReviewError("Accepted suggestion evidence is invalid")
        refs.append(raw_ref)
        run_id = source_run_id(raw_ref)
        if run_id is not None and run_id not in run_ids:
            run_ids.append(run_id)
    if not run_ids:
        raise MetaReviewError("Accepted suggestion has no source run evidence")

    runs: list[RunState] = []
    for run_id in run_ids:
        run = store.get_run(run_id)
        if run is None:
            raise MetaReviewError("Accepted suggestion source run is unavailable")
        if run.project_id != project_id:
            raise MetaReviewError(
                "Accepted suggestion source run belongs to another Project"
            )
        runs.append(run)

    repositories = {run.repository.casefold() for run in runs}
    if len(repositories) != 1:
        raise MetaReviewError(
            "Accepted suggestion source runs use different repositories"
        )

    evidence = "\n".join(
        f"- {reference.replace(chr(13), ' ').replace(chr(10), ' ')}"
        for reference in refs
    )
    body = "\n".join(
        (
            "## Proposed outcome",
            "",
            outcome,
            "",
            "## Rationale",
            "",
            rationale,
            "",
            "## Evidence references",
            evidence,
            "",
            f"Project ID: {project_id}",
            f"Suggestion ID: {suggestion_id}",
        )
    )
    return PbiCreationRequest(
        project_id=project_id,
        repository=runs[0].repository,
        title=outcome,
        body=body,
        labels=(),
    )
