from __future__ import annotations

from typing import cast

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.agent_stations import AgentStationService, AgentStationServiceError
from beehaiive.api.context import ApiRouteContext
from beehaiive.api.helpers.assets import docs_asset
from beehaiive.api.helpers.service_errors import (
    ServiceErrorPolicy,
    service_error_http_exception,
)
from beehaiive.api.models import StationIssueActionRequest
from beehaiive.dashboard.values import safe_dashboard_value

_ERROR_POLICY = ServiceErrorPolicy(
    conflict_codes=frozenset({"persistence"}),
    invalid_codes=frozenset({"invalid_action"}),
    default_status=400,
    classified_detail=False,
    redact_message=False,
)


def register_routes(app: FastAPI, context: ApiRouteContext) -> None:
    service: AgentStationService = context.agent_station_service
    require_project_access = context.require_project_access
    require_dashboard_workflow_operator = context.require_dashboard_workflow_operator
    secret_values = context.dashboard_secret_values

    @app.get("/agent-stations", response_class=FileResponse)
    def agent_stations_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return docs_asset("agent-stations.html", media_type="text/html")

    @app.get("/agent-stations.js", response_class=FileResponse)
    def agent_stations_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return docs_asset("agent-stations.js", media_type="application/javascript")

    @app.get("/projects/{project_id}/agent-stations")
    def agent_stations(
        project_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            value = service.view(project_id)
        except AgentStationServiceError as exc:
            raise _http_error(exc) from exc
        return cast(dict[str, object], safe_dashboard_value(value, secret_values))

    @app.post("/projects/{project_id}/agent-stations/issues/{issue_id}/{action}")
    def agent_station_issue_action(
        project_id: str,
        issue_id: str,
        action: str,
        request: StationIssueActionRequest,
        actor: str = Depends(require_dashboard_workflow_operator),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        try:
            value = service.act_on_issue(
                project_id, issue_id, action, actor, request.note
            )
        except AgentStationServiceError as exc:
            raise _http_error(exc) from exc
        return cast(dict[str, object], safe_dashboard_value(value, secret_values))


def _http_error(error: AgentStationServiceError) -> HTTPException:
    return service_error_http_exception(error, _ERROR_POLICY)


__all__ = ["register_routes"]
