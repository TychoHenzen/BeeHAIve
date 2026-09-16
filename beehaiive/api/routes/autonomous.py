from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from beehaiive.api.models import AutonomousRunRequest


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    service = context["autonomous_service"]
    require_mutation_access = context["require_mutation_access"]
    require_project_access = context["require_project_access"]

    @app.post("/projects/{project_id}/autonomous-runs")
    def start_autonomous_run(
        project_id: str,
        request: AutonomousRunRequest,
        _auth: None = Depends(require_mutation_access),
    ) -> dict[str, object]:
        if not request.approved:
            raise HTTPException(
                status_code=400,
                detail="Operator approval is required for autonomous runs",
            )
        try:
            return service.start(project_id, request.repository, request.pbi_number)
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="Autonomous lifecycle could not start"
            ) from exc

    @app.get("/projects/{project_id}/autonomous-runs/{run_id}")
    def autonomous_run_status(
        project_id: str,
        run_id: str,
        _access: None = Depends(require_project_access),
    ) -> dict[str, object]:
        try:
            status = service.status(run_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail="Autonomous run not found"
            ) from exc
        if status.get("project_id") != project_id:
            raise HTTPException(status_code=404, detail="Autonomous run not found")
        return status
