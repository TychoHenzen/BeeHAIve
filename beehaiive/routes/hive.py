from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..hive import HiveAcknowledgementConflict
from ..services import ServicesDep

router = APIRouter()


def _require_operator(body: dict[str, Any] | None) -> str:
    acknowledged_by = "operator"
    if (
        body is not None
        and body.get("acknowledged_by") is not None
        and body["acknowledged_by"] != acknowledged_by
    ):
        raise HTTPException(status_code=403, detail="operator acknowledgement required")
    return acknowledged_by


@router.get("/api/hive")
def hive(services: ServicesDep) -> dict[str, Any]:
    return services.hive.snapshot()


@router.get("/api/hive/events")
def hive_events(services: ServicesDep, since: str | None = None) -> dict[str, Any]:
    return services.hive.events(since)


@router.post("/api/passes/{pass_id}/acknowledge")
def acknowledge_pass(
    pass_id: str, services: ServicesDep, body: dict[str, Any] | None = None
) -> dict[str, str]:
    acknowledged_by = _require_operator(body)
    try:
        result = services.hive.acknowledge(pass_id, acknowledged_by)
    except HiveAcknowledgementConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="pass not found")
    return result
