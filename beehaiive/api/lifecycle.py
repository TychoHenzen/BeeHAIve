import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from beehaiive.api.context import ApiRuntime
from beehaiive.api.helpers.http import (
    _pbi_refinement_failure as _pbi_refinement_failure,
)
from beehaiive.operator_notifications import (
    dispatch_pending_operator_notifications,
)
from beehaiive.review import REQUIRED_CONCERNS


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "")
        if isinstance(route_path, str) and route_path.endswith("/refinement/apply"):
            return JSONResponse(
                status_code=422,
                content=_pbi_refinement_failure(
                    "request_validation",
                    "validation",
                    "Invalid PBI refinement request",
                ),
            )
        if isinstance(route_path, str) and "/refinement" in route_path:
            return JSONResponse(
                status_code=422,
                content={"detail": "Invalid PBI refinement request"},
            )
        if isinstance(route_path, str) and (
            route_path.endswith("/actions") or route_path.endswith("/dashboard")
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "Invalid dashboard request"},
            )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: HTTPException
    ) -> Response:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "")
        if isinstance(route_path, str) and route_path.endswith("/refinement/apply"):
            unauthorized = exc.status_code in {401, 403}
            return JSONResponse(
                status_code=exc.status_code,
                content=_pbi_refinement_failure(
                    "authorization_failed" if unauthorized else "request_rejected",
                    "authorization" if unauthorized else "preflight",
                    "PBI refinement authorization failed"
                    if unauthorized
                    else "PBI refinement could not be applied",
                ),
            )
        return await http_exception_handler(request, exc)


def register_background_handlers(
    runtime: ApiRuntime,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    agent_worker = runtime.agent_worker
    orchestrator = runtime.orchestrator
    scheduler = runtime.scheduler
    require_review_adapters = runtime.require_review_adapters
    review_service = runtime.review_service
    review_repair_service = runtime.review_repair_service

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await asyncio.to_thread(
            orchestrator.store.recover_interrupted_operator_notifications
        )
        await asyncio.to_thread(
            dispatch_pending_operator_notifications, orchestrator.store
        )

        if require_review_adapters:
            missing = [
                concern.value
                for concern in REQUIRED_CONCERNS
                if concern not in review_service.readers
            ]
            if review_service.provider is None or missing:
                configured = "pull-request provider"
                if missing:
                    configured = f"{configured} and readers: {', '.join(missing)}"
                raise RuntimeError(
                    f"Production review adapters are not configured: {configured}"
                )
            validate_configuration = getattr(
                review_service.provider, "validate_configuration", None
            )
            if callable(validate_configuration):
                validate_configuration()

        if review_repair_service is not None:
            review_repair_service.recover()

        if agent_worker is not None:
            if scheduler is not None:
                agent_worker.recover(scheduler.project_ids)
            else:
                agent_worker.recover()
            if scheduler is not None and scheduler.config.enabled:
                scheduler.start()

        try:
            yield
        finally:
            if agent_worker is not None:
                if scheduler is not None:
                    scheduler.shutdown()
                agent_worker.shutdown()

    return lifespan
