from __future__ import annotations

from typing import Any, cast

from fastapi import HTTPException

from ..config import CoreConfigurationError
from ..project import ProjectDataError
from ..rest import GithubRestError
from ..services import Services
from ..workflows import assign_layered_layout, validate_workflow


def definition_from_body(body: dict[str, Any]) -> Any:
    value = body.get("definition", body)
    if not isinstance(value, dict):
        return value
    definition = dict(cast(dict[str, Any], value))
    for field in ("name", "source_prompt"):
        if field in body:
            definition[field] = body[field]
    return definition


def status_options(services: Services) -> tuple[str, ...]:
    try:
        snapshot = services.snapshots.get_snapshot()
    except (CoreConfigurationError, ProjectDataError, GithubRestError, OSError):
        return ()
    return tuple(
        column.status for column in snapshot.columns if column.status != "No status"
    )


def validated(body: dict[str, Any], services: Services) -> Any:
    definition = definition_from_body(body)
    return validate_workflow(
        definition,
        skills_dirs=services.config.skills_dirs,
        status_options=status_options(services),
    )


def prepared_definition(body: dict[str, Any], services: Services) -> dict[str, Any]:
    validation = validated(body, services)
    if not validation.valid:
        raise HTTPException(status_code=422, detail=validation.as_dict())
    return assign_layered_layout(validation.definition)
