from __future__ import annotations

from fastapi import APIRouter

from . import agent_runs, agents, generation, hive, project, system, workflows

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    project.router,
    agents.router,
    agent_runs.router,
    hive.router,
    generation.router,
    workflows.router,
    system.health_router,
)
