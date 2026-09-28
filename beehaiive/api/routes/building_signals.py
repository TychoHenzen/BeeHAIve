from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.api.models import (
    BuildingSignalAssignmentRequest,
    BuildingSignalGenerateRequest,
    BuildingSignalRuleRequest,
)
from beehaiive.building_signal import BuildingSignalServiceError
from beehaiive.building_signal_service import BuildingSignalService
from beehaiive.contract_types.validation import _redact_text
from beehaiive.service_failures import FailureCategory


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    service: BuildingSignalService = context["building_signal_service"]
    require_project_access = context["require_project_access"]
    require_dashboard_workflow_operator = context["require_dashboard_workflow_operator"]
    docs_directory = Path(__file__).resolve().parents[3] / "docs"

    @app.get("/building-signal-design", response_class=FileResponse)
    def building_signal_design() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "building-signal.html", media_type="text/html"
        )

    @app.get("/projects/{project_id}/building-signals")
    def building_signal_list(
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return {
                "building_signals": [
                    record.as_dict() for record in service.list(project_id)
                ]
            }
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.get("/projects/{project_id}/building-signals/{rule_id}")
    def building_signal_detail(
        project_id: str,
        rule_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            record = service.get(project_id, rule_id)
            if record is None:
                raise BuildingSignalServiceError(
                    "not_found", "Building signal rule not found"
                )
            return record.as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/building-signals/generate")
    def building_signal_generate(
        project_id: str,
        request: BuildingSignalGenerateRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.generate(project_id, request.prompt)
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/building-signals")
    def building_signal_save(
        project_id: str,
        request: BuildingSignalRuleRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.create(project_id, request.rule).as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.put("/projects/{project_id}/building-signals/{rule_id}")
    def building_signal_update(
        project_id: str,
        rule_id: str,
        request: BuildingSignalRuleRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.update(project_id, rule_id, request.rule).as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/building-signals/{rule_id}/confirm")
    def building_signal_confirm(
        project_id: str,
        rule_id: str,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.confirm(project_id, rule_id).as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/building-signals/{rule_id}/assign")
    def building_signal_assign(
        project_id: str,
        rule_id: str,
        request: BuildingSignalAssignmentRequest,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.assign(project_id, rule_id, request.building_id).as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc

    @app.post("/projects/{project_id}/building-signals/{rule_id}/evaluate")
    def building_signal_evaluate(
        project_id: str,
        rule_id: str,
        _actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            return service.evaluate(project_id, rule_id).as_dict()
        except BuildingSignalServiceError as exc:
            raise _http_error(exc) from exc


def _http_error(error: BuildingSignalServiceError) -> HTTPException:
    if error.code == "not_found":
        status = 404
    elif (
        getattr(error, "category", None)
        in {
            FailureCategory.MODEL.value,
            FailureCategory.TARGET_PROVIDER.value,
            FailureCategory.PERSISTENCE.value,
        }
        or error.code == "model_unavailable"
    ):
        status = 503
    elif error.code in {"conflict", "invalid_state", "persistence"}:
        status = 409
    else:
        status = 422
    category = error.category
    return HTTPException(
        status_code=status,
        detail={
            "code": error.code,
            "failure_class": category,
            "message": _redact_text(str(error), 512),
        },
    )


__all__ = ["register_routes"]
