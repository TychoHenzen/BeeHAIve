from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.api.context import ApiRouteContext
from beehaiive.api.helpers.assets import docs_asset
from beehaiive.api.helpers.service_errors import (
    ServiceErrorPolicy,
    service_error_http_exception,
)
from beehaiive.api.models import (
    BehaviorAssignmentRequest,
    BehaviorBindingsRequest,
    BehaviorGenerateRequest,
    BehaviorSaveRequest,
)
from beehaiive.behavior_model import BehaviorModelError
from beehaiive.behavior_service import BehaviorService, BehaviorServiceError
from beehaiive.service_failures import FailureCategory

_ERROR_POLICY = ServiceErrorPolicy(
    unavailable_categories=frozenset(
        {
            FailureCategory.MODEL.value,
            FailureCategory.TARGET_PROVIDER.value,
            FailureCategory.PERSISTENCE.value,
        }
    ),
    unavailable_codes=frozenset({"model_unavailable"}),
    conflict_codes=frozenset({"state_conflict", "persistence", "invalid_checkpoint"}),
    default_category=FailureCategory.MODEL,
)


def register_routes(app: FastAPI, context: ApiRouteContext) -> None:
    service: BehaviorService = context.behavior_service
    require_project_access = context.require_project_access
    require_dashboard_workflow_operator = context.require_dashboard_workflow_operator

    @app.get("/behavior-design", response_class=FileResponse)
    def behavior_design() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return docs_asset("behavior-design.html", media_type="text/html")

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
    return service_error_http_exception(error, _ERROR_POLICY)


__all__ = ["register_routes"]
