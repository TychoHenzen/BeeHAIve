from __future__ import annotations

from fastapi import APIRouter

from . import archon_proxy, archon_tools, sessions, system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    system.health_router,
    sessions.router,
    archon_tools.router,
    archon_proxy.router,
)
