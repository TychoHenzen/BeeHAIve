import os
from collections.abc import Collection
from dataclasses import replace

from beehaiive.routing import RoutingConfig


def _configured_project_ids(
    project_ids: Collection[str] | None,
) -> frozenset[str]:
    if project_ids is not None:
        return frozenset(project_ids)
    configured = os.environ.get("BEEHAIIVE_ALLOWED_PROJECTS")
    if configured:
        return frozenset(
            project_id.strip()
            for project_id in configured.split(",")
            if project_id.strip()
        )
    owner = os.environ.get("GITHUB_PROJECT_OWNER")
    number = os.environ.get("GITHUB_PROJECT_NUMBER")
    if owner and number:
        return frozenset({f"{owner}:{number}"})
    return frozenset()


def _routing_config_from_environment() -> RoutingConfig:
    config = RoutingConfig()
    override = os.environ.get("BEEHAIIVE_CODEX_MODEL", "").strip()
    if not override:
        return config
    return replace(
        config,
        writer=replace(config.writer, model=override),
        triage=tuple(replace(spec, model=override) for spec in config.triage),
    )


__all__ = ["_configured_project_ids", "_routing_config_from_environment"]
