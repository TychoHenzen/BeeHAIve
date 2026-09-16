from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from beehaiive.api.helpers.http import _handle_store_error as _handle_store_error
from beehaiive.api.models import RoutingAttemptRequest as RoutingAttemptRequest
from beehaiive.routing import RoutingError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    orchestrator = context["orchestrator"]
    require_mutation_access = context["require_mutation_access"]
    require_routing_run_access = context["require_routing_run_access"]
    routing_service = context["routing_service"]
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
