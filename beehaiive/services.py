from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, TypedDict

from fastapi import Depends, Request

from .agents import AgentService
from .config import CoreConfig
from .database import SnapshotDatabase
from .hive import HiveProjection
from .project import ProjectProvider
from .snapshot import ProjectSnapshotService
from .workflow_generator import WorkflowGenerationService
from .workflows import WorkflowStore


class AppOverrides(TypedDict, total=False):
    database: SnapshotDatabase
    service: ProjectSnapshotService
    workflow_generator: WorkflowGenerationService
    agent_service: AgentService


@dataclass(frozen=True)
class Services:
    config: CoreConfig
    database: SnapshotDatabase
    owned_database: bool
    snapshots: ProjectSnapshotService
    workflows: WorkflowStore
    generator: WorkflowGenerationService
    agents: AgentService
    hive: HiveProjection


def _provider(config: CoreConfig, database: SnapshotDatabase) -> ProjectProvider:
    from .rest import GithubRestClient

    return ProjectProvider(config, GithubRestClient(config.github_token, database))


def _snapshots(
    config: CoreConfig, database: SnapshotDatabase, overrides: AppOverrides
) -> ProjectSnapshotService:
    return overrides.get("service") or ProjectSnapshotService(
        _provider(config, database),
        database,
        minimum_refresh_seconds=config.refresh_seconds,
    )


def _assemble(
    config: CoreConfig,
    database: SnapshotDatabase,
    owned: bool,
    overrides: AppOverrides,
) -> Services:
    snapshots = _snapshots(config, database, overrides)
    workflows = WorkflowStore(database)
    generator = overrides.get("workflow_generator") or WorkflowGenerationService(
        config, snapshots
    )
    agents = overrides.get("agent_service") or AgentService(
        config, database, workflows, snapshots
    )
    return Services(
        config,
        database,
        owned,
        snapshots,
        workflows,
        generator,
        agents,
        HiveProjection(database),
    )


def build_services(config: CoreConfig, overrides: AppOverrides) -> Services:
    owned = "database" not in overrides
    database = overrides.get("database") or SnapshotDatabase(config.db_path)
    try:
        return _assemble(config, database, owned, overrides)
    except BaseException:
        if owned:
            database.close()
        raise


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
