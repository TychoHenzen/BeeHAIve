from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx2
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from .archon.cli import ArchonCli
from .archon.pipeline import PipelineReader
from .archon.triggers import Scheduler, TriggerStore
from .config import CoreConfig
from .guard import LocalRequestGuard
from .routes import ROUTERS
from .routes.system import WEB_DIR
from .sessions import SessionIndex


class _RevalidatedFiles(StaticFiles):
    """Static files the browser must recheck, so an upgrade shows on reload."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    config: CoreConfig | None = None,
    index: SessionIndex | None = None,
    *,
    archon_http: httpx2.AsyncClient | None = None,
    archon_cli: ArchonCli | None = None,
    scheduler: Scheduler | None = None,
    pipeline: PipelineReader | None = None,
) -> FastAPI:
    config = config or CoreConfig.from_environment()
    session_index = index or SessionIndex(
        config.projects_dir, timedelta(hours=config.lookback_hours)
    )
    archon = config.archon
    http = archon_http or httpx2.AsyncClient(base_url=archon.url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        session_index.start()
        try:
            yield
        finally:
            session_index.stop()
            await http.aclose()

    app = FastAPI(title="BeeHAIve", lifespan=lifespan)
    app.state.index = session_index
    app.state.archon_settings = archon
    app.state.archon_http = http
    app.state.archon_cli = archon_cli or ArchonCli(archon.binary)
    app.state.trigger_store = TriggerStore(archon.triggers_dir)
    app.state.scheduler = scheduler or Scheduler(archon.binary)
    app.state.pipeline = pipeline or PipelineReader(archon.home)
    app.add_middleware(LocalRequestGuard)
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/web", _RevalidatedFiles(directory=WEB_DIR), name="web")
    return app
