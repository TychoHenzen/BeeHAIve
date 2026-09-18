import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.models import (
    DashboardSettingsRequest as DashboardSettingsRequest,
)
from beehaiive.api.models import RoutingAttemptRequest as RoutingAttemptRequest
from beehaiive.routing import RoutingError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    orchestrator = context["orchestrator"]
    require_mutation_access = context["require_mutation_access"]
    require_dashboard_settings_mutation = context["require_dashboard_settings_mutation"]
    require_routing_run_access = context["require_routing_run_access"]
    routing_service = context["routing_service"]
    configured_projects = context["configured_projects"]
    scheduler = context["scheduler"]
    runtime_settings = context["runtime_settings"]
    docs_directory = Path(__file__).resolve().parents[3] / "docs"
    dashboard_view_modules = {
        "actions",
        "details",
        "dom",
        "graph",
        "pbi",
        "repository",
        "summary",
    }

    @app.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": "Hello World"}

    @app.get("/hello/{name}")
    async def say_hello(name: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": f"Hello {name}"}

    @app.get("/runs/{run_id}/routing")
    def routing_problem(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        _auth: None = Depends(require_routing_run_access),
    ) -> dict[str, object]:
        try:
            return routing_service.snapshot(run_id).as_dict()
        except RoutingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/routing/attempts")
    def record_routing_attempt(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        request: RoutingAttemptRequest,
        _auth: None = Depends(require_routing_run_access),
    ) -> dict[str, object]:
        try:
            return routing_service.record(
                run_id,
                request.outcome,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                failure_context=request.failure_context,
                recursive_spawn_depth=request.recursive_spawn_depth,
            ).as_dict()
        except RoutingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/dashboard", response_class=FileResponse)
    def dashboard() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard.html",
            media_type="text/html",
        )

    @app.get("/dashboard/config")
    def dashboard_config() -> dict[str, object]:
        return {"projects": sorted(configured_projects)}

    @app.get("/dashboard/settings")
    def dashboard_settings() -> dict[str, object]:
        persisted = orchestrator.store.get_runtime_settings()
        return {
            "projects": sorted(configured_projects),
            "workflow_id": persisted.get("workflow_id"),
            "repository": os.environ.get("BEEHAIIVE_AGENT_REPOSITORY_NAME"),
            "scheduler": (
                {
                    "enabled": scheduler.config.enabled,
                    "poll_interval_seconds": scheduler.config.poll_interval_seconds,
                    "max_concurrency": scheduler.config.max_concurrency,
                }
                if scheduler is not None
                else persisted.get("scheduler")
            ),
            "restart_persistence": "server_state",
        }

    @app.put("/dashboard/settings")
    def update_dashboard_settings(
        request: DashboardSettingsRequest,
        _auth: None = Depends(require_dashboard_settings_mutation),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for dashboard settings",
            )
        updates: dict[str, object] = {}
        if request.projects is not None:
            projects = {
                project.strip() for project in request.projects if project.strip()
            }
            if not projects or any(
                project.count(":") != 1
                or not project.rsplit(":", 1)[1].isdigit()
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", project.split(":", 1)[0])
                for project in projects
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Projects must use owner:number format",
                )
            if scheduler is not None:
                scheduler.configure_projects(projects)
            configured_projects.clear()
            configured_projects.update(projects)
            updates["projects"] = sorted(projects)
        if "workflow_id" in request.model_fields_set:
            workflow_id = request.workflow_id.strip() if request.workflow_id else ""
            if workflow_id and not re.fullmatch(
                r"[a-z0-9][a-z0-9-]{0,127}", workflow_id
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Workflow IDs must use lowercase letters, digits, and hyphens"
                    ),
                )
            updates["workflow_id"] = workflow_id
        if updates:
            runtime_settings.update(orchestrator.store.update_runtime_settings(updates))
        return dashboard_settings()

    @app.get("/dashboard.js", response_class=FileResponse)
    def dashboard_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard.js",
            media_type="application/javascript",
        )

    @app.get("/dashboard-client.mjs", response_class=FileResponse)
    def dashboard_client_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard-client.mjs",
            media_type="application/javascript",
        )

    @app.get("/dashboard-ui.mjs", response_class=FileResponse)
    def dashboard_ui_module() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard-ui.mjs",
            media_type="application/javascript",
        )

    @app.get("/dashboard-demo.mjs", response_class=FileResponse)
    def dashboard_demo_module() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard-demo.mjs",
            media_type="application/javascript",
        )

    @app.get("/dashboard-view.mjs", response_class=FileResponse)
    def dashboard_view_script() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(
            docs_directory / "dashboard-view.mjs",
            media_type="application/javascript",
        )

    @app.get("/dashboard-view/{module}.mjs", response_class=FileResponse)
    def dashboard_view_module(module: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        if module not in dashboard_view_modules:
            raise HTTPException(
                status_code=404, detail="Dashboard view module not found"
            )
        return FileResponse(
            docs_directory / "dashboard-view" / f"{module}.mjs",
            media_type="application/javascript",
        )

    @app.post("/projects/{project_id}/sync")
    def synchronize(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(
            lambda: orchestrator.synchronize(project_id, force_refresh=True)
        )
