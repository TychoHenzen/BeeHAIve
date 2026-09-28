from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.api.models import (
    BehaviorAssignmentRequest,
    BehaviorBindingsRequest,
    BehaviorGenerateRequest,
    BehaviorSaveRequest,
)
from beehaiive.behavior_model import BehaviorModelError
from beehaiive.behavior_service import BehaviorService, BehaviorServiceError
from beehaiive.contract_types.validation import _redact_text
from beehaiive.service_failures import FailureCategory


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    service: BehaviorService = context["behavior_service"]
    require_project_access = context["require_project_access"]
    require_dashboard_workflow_operator = context["require_dashboard_workflow_operator"]
    docs_directory = Path(__file__).resolve().parents[3] / "docs"

    @app.get("/behavior-design", response_class=FileResponse)
    def behavior_design() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "behavior-design.html", media_type="text/html"
        )

    @app.get("/projects/{project_id}/behaviors")
    def behavior_list(
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return {
                "behaviors": [record.as_dict() for record in service.list(project_id)]
            }
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.get("/projects/{project_id}/behaviors/{behavior_id}")
    def behavior_detail(
        project_id: str,
        behavior_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.get(project_id, behavior_id).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/behaviors/generate")
    def behavior_generate(
        project_id: str,
        request: BehaviorGenerateRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.generate(request.prompt)
        except (BehaviorModelError, BehaviorServiceError) as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/behaviors")
    def behavior_save(
        project_id: str,
        request: BehaviorSaveRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.create(
                project_id, request.definition, request.bindings
            ).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.put("/projects/{project_id}/behaviors/{behavior_id}/bindings")
    def behavior_bind(
        project_id: str,
        behavior_id: str,
        request: BehaviorBindingsRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.bind(project_id, behavior_id, request.bindings).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/behaviors/{behavior_id}/confirm")
    def behavior_confirm(
        project_id: str,
        behavior_id: str,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.confirm(project_id, behavior_id).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/behaviors/{behavior_id}/assign")
    def behavior_assign(
        project_id: str,
        behavior_id: str,
        request: BehaviorAssignmentRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.assign(project_id, behavior_id, request.unit_id).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/behaviors/{behavior_id}/run")
    def behavior_run(
        project_id: str,
        behavior_id: str,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.run(project_id, behavior_id).as_dict()
        except BehaviorServiceError as exc:
            raise _http_error(exc) from exc


def _http_error(error: BehaviorModelError | BehaviorServiceError) -> HTTPException:
    if error.code == "not_found":
        status = 404
    elif (
        getattr(error, "category", None)
        in {
            FailureCategory.MODEL,
            FailureCategory.TARGET_PROVIDER,
            FailureCategory.PERSISTENCE,
        }
        or error.code == "model_unavailable"
    ):
        status = 503
    elif error.code in {"state_conflict", "persistence", "invalid_checkpoint"}:
        status = 409
    else:
        status = 422
    category = getattr(error, "category", FailureCategory.MODEL)
    if isinstance(category, FailureCategory):
        category = category.value
    return HTTPException(
        status_code=status,
        detail={
            "code": error.code,
            "failure_class": str(category),
            "message": _redact_text(str(error), 512),
        },
    )


__all__ = ["register_routes"]
