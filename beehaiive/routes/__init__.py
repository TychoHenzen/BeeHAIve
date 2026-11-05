from __future__ import annotations

from fastapi import APIRouter

from . import sessions, system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    system.health_router,
    sessions.router,
)
