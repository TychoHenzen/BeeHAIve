from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from ..services import ServicesDep

router = APIRouter()


@dataclass(frozen=True)
class LogQuery:
    pass_id: str | None
    limit: int


def log_query(pass_id: str | None = None, limit: int = 100) -> LogQuery:
    return LogQuery(pass_id=pass_id, limit=limit)


@router.get("/api/agents/{agent_id}/logs")
def agent_logs(
    agent_id: str,
    query: Annotated[LogQuery, Depends(log_query)],
    services: ServicesDep,
) -> dict[str, Any]:
    value = services.agents.logs(agent_id, pass_id=query.pass_id, limit=query.limit)
    if value is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return value


@router.get("/api/agents/{agent_id}/history")
def agent_history(agent_id: str, services: ServicesDep) -> dict[str, Any]:
    value = services.agents.get_agent(agent_id)
    if value is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"passes": value["history"], "alerts": value["alerts"]}
