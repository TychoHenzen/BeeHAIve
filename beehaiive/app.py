from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from .config import CoreConfig
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
    config: CoreConfig | None = None, index: SessionIndex | None = None
) -> FastAPI:
    config = config or CoreConfig.from_environment()
    session_index = index or SessionIndex(
        config.projects_dir, timedelta(hours=config.lookback_hours)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        session_index.start()
        try:
            yield
        finally:
            session_index.stop()

    app = FastAPI(title="BeeHAIve", lifespan=lifespan)
    app.state.index = session_index
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/web", _RevalidatedFiles(directory=WEB_DIR), name="web")
    return app
