from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Unpack

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import CoreConfig
from .routes import ROUTERS
from .routes.system import WEB_DIR
from .services import AppOverrides, Services, build_services


def _lifespan(
    services: Services,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        services.agents.shutdown()
        if services.owned_database:
            services.database.close()

    return lifespan


def create_app(
    config: CoreConfig | None = None, **overrides: Unpack[AppOverrides]
) -> FastAPI:
    services = build_services(config or CoreConfig.from_environment(), overrides)
    app = FastAPI(title="BeeHAIve Core", lifespan=_lifespan(services))
    app.state.services = services
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="core-web")
    return app
