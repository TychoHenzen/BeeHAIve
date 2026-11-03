from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from ..agents import AgentConflict, AgentError
from ..services import ServicesDep

router = APIRouter()


def _or_404(value: Any) -> Any:
    if value is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return value


def _guarded(
    call: Callable[[], Any], failures: tuple[tuple[type[Exception], int], ...]
) -> Any:
    try:
        return call()
    except Exception as error:
        for failure_type, status_code in failures:
            if isinstance(error, failure_type):
                raise HTTPException(
                    status_code=status_code, detail=str(error)
                ) from error
        raise


@router.get("/api/agents")
def agents(services: ServicesDep) -> dict[str, Any]:
    return {"agents": services.agents.list_agents()}


@router.post("/api/agents")
def create_agent(body: dict[str, Any], services: ServicesDep) -> dict[str, Any]:
    return _guarded(
        lambda: services.agents.create_agent(body),
        ((AgentConflict, 409), (AgentError, 422), (OSError, 409), (RuntimeError, 409)),
    )


@router.get("/api/agents/{agent_id}")
def agent(agent_id: str, services: ServicesDep) -> dict[str, Any]:
    return _or_404(services.agents.get_agent(agent_id))


@router.patch("/api/agents/{agent_id}")
def update_agent(
    agent_id: str, body: dict[str, Any], services: ServicesDep
) -> dict[str, Any]:
    value = _guarded(
        lambda: services.agents.update_agent(agent_id, body),
        ((AgentConflict, 409), (AgentError, 422)),
    )
    return _or_404(value)


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, services: ServicesDep) -> dict[str, bool]:
    outcome = services.agents.delete_agent(agent_id)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="agent not found")
    if outcome == "running":
        raise HTTPException(
            status_code=409, detail="agent must be stopped before deletion"
        )
    return {"deleted": True}


@router.post("/api/agents/{agent_id}/start")
def start_agent(agent_id: str, services: ServicesDep) -> dict[str, Any]:
    value = _guarded(
        lambda: services.agents.start_agent(agent_id),
        ((AgentConflict, 409), (AgentError, 422), (OSError, 422), (RuntimeError, 422)),
    )
    return _or_404(value)


@router.post("/api/agents/{agent_id}/stop")
def stop_agent(agent_id: str, services: ServicesDep) -> dict[str, Any]:
    return _or_404(services.agents.stop_agent(agent_id))


@router.post("/api/agents/{agent_id}/reset")
def reset_agent(agent_id: str, services: ServicesDep) -> dict[str, Any]:
    value = _guarded(
        lambda: services.agents.reset_agent(agent_id),
        ((AgentConflict, 409),),
    )
    return _or_404(value)
