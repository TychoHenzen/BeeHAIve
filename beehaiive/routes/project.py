from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from fastapi import APIRouter, HTTPException

from ..agents import item_key
from ..config import CoreConfigurationError
from ..models import ProjectColumn, ProjectSnapshot
from ..project import ProjectDataError
from ..rest import GithubRestError
from ..services import Services, ServicesDep

router = APIRouter()


def snapshot_payload(
    snapshot: ProjectSnapshot, holders: Mapping[str, Any]
) -> dict[str, Any]:
    payload = snapshot.as_dict()
    columns = cast(list[dict[str, Any]], payload["columns"])
    for source_column, column in zip(snapshot.columns, columns, strict=True):
        _attach_holders(source_column, column, holders)
    return payload


def _attach_holders(
    source_column: ProjectColumn, column: dict[str, Any], holders: Mapping[str, Any]
) -> None:
    for source_item, item in zip(source_column.items, column["items"], strict=True):
        holder = holders.get(item_key(source_item))
        if holder is not None:
            item["holder"] = holder


def read_snapshot(services: Services, *, refresh: bool = False) -> dict[str, Any]:
    try:
        if refresh:
            snapshot = services.snapshots.request_refresh()
        else:
            snapshot = services.snapshots.get_snapshot()
    except (CoreConfigurationError, ProjectDataError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GithubRestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return snapshot_payload(snapshot, services.agents.holders())


@router.get("/api/project")
def project(services: ServicesDep) -> dict[str, object]:
    return read_snapshot(services)


@router.post("/api/project/refresh")
def refresh(services: ServicesDep) -> dict[str, object]:
    return read_snapshot(services, refresh=True)
