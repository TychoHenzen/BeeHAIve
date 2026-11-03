from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services import ServicesDep
from .workflow_input import prepared_definition, validated

router = APIRouter()


@router.get("/api/workflows")
def workflows(services: ServicesDep) -> dict[str, Any]:
    return {"workflows": services.workflows.list_workflows()}


@router.get("/api/workflows/{workflow_id}")
def workflow(workflow_id: int, services: ServicesDep) -> dict[str, Any]:
    value = services.workflows.get_workflow(workflow_id)
    if value is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return value


@router.post("/api/workflows/validate")
def validate_workflow_route(
    body: dict[str, Any], services: ServicesDep
) -> dict[str, Any]:
    return validated(body, services).as_dict()


@router.post("/api/workflows")
def create_workflow(body: dict[str, Any], services: ServicesDep) -> dict[str, Any]:
    prepared = prepared_definition(body, services)
    name = str(body.get("name", prepared.get("name", ""))).strip()
    source_prompt = str(body.get("source_prompt", prepared.get("source_prompt", "")))
    if not name:
        raise HTTPException(status_code=422, detail="workflow name is required")
    return services.workflows.create_workflow(name, source_prompt, prepared)


@router.post("/api/workflows/{workflow_id}/revisions")
def add_workflow_revision(
    workflow_id: int, body: dict[str, Any], services: ServicesDep
) -> dict[str, Any]:
    prepared = prepared_definition(body, services)
    result = services.workflows.add_revision(
        workflow_id,
        str(body.get("source_prompt", prepared.get("source_prompt", ""))),
        prepared,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return result


@router.delete("/api/workflows/{workflow_id}")
def delete_workflow(workflow_id: int, services: ServicesDep) -> dict[str, bool]:
    outcome = services.workflows.delete_workflow(workflow_id)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="workflow not found")
    if outcome == "assigned":
        raise HTTPException(
            status_code=409,
            detail="workflow cannot be deleted while an agent is assigned",
        )
    return {"deleted": True}
