from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException

from ..services import ServicesDep

router = APIRouter()


@dataclass(frozen=True)
class GenerationRequest:
    name: str
    prompt: str
    parameters: list[dict[str, Any]]


def parse_generation_body(body: dict[str, Any]) -> GenerationRequest:
    name, prompt, parameters = _required_fields(body)
    return GenerationRequest(name, prompt, _parameter_objects(parameters))


def _required_fields(body: dict[str, Any]) -> tuple[str, str, list[Any]]:
    name = str(body.get("name", "")).strip()
    prompt = str(body.get("prompt", ""))
    parameters = body.get("parameters", [])
    if not name or not prompt or not isinstance(parameters, list):
        raise HTTPException(
            status_code=422,
            detail="name, prompt, and a parameter list are required",
        )
    return name, prompt, cast(list[Any], parameters)


def _parameter_objects(parameters: list[Any]) -> list[dict[str, Any]]:
    if not all(isinstance(parameter, dict) for parameter in parameters):
        raise HTTPException(status_code=422, detail="parameters must be objects")
    return [cast(dict[str, Any], parameter) for parameter in parameters]


@router.post("/api/workflows/generate")
def generate_workflow(body: dict[str, Any], services: ServicesDep) -> dict[str, Any]:
    request = parse_generation_body(body)
    return services.generator.start(request.name, request.prompt, request.parameters)


@router.get("/api/workflows/generate/{job_id}")
def workflow_generation(job_id: str, services: ServicesDep) -> dict[str, Any]:
    job = services.generator.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="generation job not found")
    return job
