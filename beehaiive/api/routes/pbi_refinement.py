from collections.abc import Callable
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Path as FastAPIPath
from fastapi.responses import JSONResponse

from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.helpers.http import (
    _pbi_refinement_failure as _pbi_refinement_failure,
)
from beehaiive.api.models import (
    PbiRefinementAnswerRequest as PbiRefinementAnswerRequest,
)
from beehaiive.api.models import PbiRefinementApplyRequest as PbiRefinementApplyRequest
from beehaiive.api.models import (
    PbiRefinementCompleteRequest as PbiRefinementCompleteRequest,
)
from beehaiive.api.models import (
    PbiRefinementFailureRequest as PbiRefinementFailureRequest,
)
from beehaiive.api.models import (
    PbiRefinementReopenRequest as PbiRefinementReopenRequest,
)
from beehaiive.api.models import PbiRefinementStartRequest as PbiRefinementStartRequest
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementUpdateRequest,
    PbiRefinementUpdateResult,
)


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    orchestrator = context["orchestrator"]
    refinement_path = context["refinement_path"]
    refinement_secret_values = context["refinement_secret_values"]
    require_refinement_operator = context["require_refinement_operator"]

    @app.post(refinement_path)
    def start_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRefinementStartRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.create_pbi_refinement_attempt(
                project_id,
                repository,
                pbi_number,
                [question.model_dump() for question in request.questions],
                operator_role=operator_role,
                secret_values=refinement_secret_values,
            )
        )
        return attempt.as_dict()

    @app.get(refinement_path)
    def get_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.get_pbi_refinement_attempt(
                project_id,
                repository,
                pbi_number,
                operator_role=operator_role,
            )
        )
        if attempt is None:
            raise HTTPException(status_code=404, detail="PBI refinement not found")
        return attempt.as_dict()

    @app.post(refinement_path + "/questions/{question_id}/answer")
    def answer_pbi_refinement_question(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        question_id: str,
        request: PbiRefinementAnswerRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.answer_pbi_refinement_question(
                project_id,
                repository,
                pbi_number,
                question_id,
                request.answer,
                expected_revision=request.expected_revision,
                evidence_refs=request.evidence_refs,
                operator_role=operator_role,
                secret_values=refinement_secret_values,
            )
        )
        return attempt.as_dict()

    @app.post(refinement_path + "/complete")
    def complete_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRefinementCompleteRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.complete_pbi_refinement_attempt(
                project_id,
                repository,
                pbi_number,
                expected_revision=request.expected_revision,
                summary=request.summary,
                evidence_refs=request.evidence_refs,
                operator_role=operator_role,
                secret_values=refinement_secret_values,
            )
        )
        return attempt.as_dict()

    @app.post(refinement_path + "/fail")
    def fail_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRefinementFailureRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.fail_pbi_refinement_attempt(
                project_id,
                repository,
                pbi_number,
                expected_revision=request.expected_revision,
                reason=request.reason,
                retryable=request.retryable,
                operator_role=operator_role,
                secret_values=refinement_secret_values,
            )
        )
        return attempt.as_dict()

    @app.post(refinement_path + "/reopen")
    def reopen_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRefinementReopenRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> dict[str, object]:
        attempt = _handle_store_error(
            lambda: orchestrator.store.reopen_pbi_refinement_attempt(
                project_id,
                repository,
                pbi_number,
                reason=request.reason,
                questions=[question.model_dump() for question in request.questions],
                operator_role=operator_role,
                secret_values=refinement_secret_values,
            )
        )
        return attempt.as_dict()

    @app.post(refinement_path + "/apply")
    def apply_pbi_refinement(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: str,
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRefinementApplyRequest,
        operator_role: str = Depends(require_refinement_operator),
    ) -> JSONResponse:
        del operator_role
        provider_method = getattr(orchestrator.provider, "apply_pbi_refinement", None)
        if not callable(provider_method):
            raise HTTPException(
                status_code=503, detail="PBI refinement mutations are unavailable"
            )
        apply_mutation = cast(
            Callable[[PbiRefinementUpdateRequest], PbiRefinementUpdateResult],
            provider_method,
        )
        mutation_request = PbiRefinementUpdateRequest(
            project_id=project_id,
            repository=repository,
            pbi_number=pbi_number,
            sections=request.sections,
            priority_label=request.priority_label,
            effort_label=request.effort_label,
            standard_labels=tuple(request.standard_labels),
        )
        try:
            result = apply_mutation(mutation_request)
        except PbiRefinementMutationError as exc:
            pending_step = (
                "validation"
                if exc.code.startswith("invalid_")
                or exc.code in {"body_too_large", "duplicate_section_heading"}
                else "preflight"
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=_pbi_refinement_failure(
                    exc.code, pending_step, "PBI refinement could not be applied"
                ),
            )
        status_code = 200 if result.status == "complete" else 202
        return JSONResponse(status_code=status_code, content=result.as_dict())
