import os
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore, Stage
from beehaiive.models import RunState
from beehaiive.provider import ProviderError
from beehaiive.storage import StoreError


class AdvanceRequest(BaseModel):
    target: Stage


class HandoffRequest(BaseModel):
    branch: str
    base_branch: str | None = None
    body: str = ""


class FailureRequest(BaseModel):
    error: str


def create_app(
    store: OrchestratorStore | None = None,
    orchestrator: Orchestrator | None = None,
) -> FastAPI:
    if orchestrator is None:
        if store is None:
            database = os.environ.get("BEEHAIIVE_STATE_DB", ".beehaiive/state.db")
            store = OrchestratorStore(
                database if database == ":memory:" else Path(database)
            )
        orchestrator = Orchestrator(store, EnvironmentGitHubProvider())

    app = FastAPI(title="BeeHAIve")

    @app.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": "Hello World"}

    @app.get("/hello/{name}")
    async def say_hello(name: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"message": f"Hello {name}"}

    @app.post("/projects/{project_id}/sync")
    def synchronize(project_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(lambda: orchestrator.synchronize(project_id))

    @app.get("/projects/{project_id}")
    def project_state(project_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _handle_store_error(lambda: orchestrator.store.project_state(project_id))

    @app.post("/projects/{project_id}/repositories/{repository:path}/claim")
    def claim(  # pyright: ignore[reportUnusedFunction]
        project_id: str, repository: str
    ) -> dict[str, object] | None:
        run = _handle_store_error(lambda: orchestrator.claim(project_id, repository))
        return None if run is None else _run_dict(run)

    @app.post("/runs/{run_id}/advance")
    def advance(  # pyright: ignore[reportUnusedFunction]
        run_id: str, request: AdvanceRequest
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(lambda: orchestrator.advance(run_id, request.target))
        )

    @app.post("/runs/{run_id}/handoff")
    def handoff(  # pyright: ignore[reportUnusedFunction]
        run_id: str, request: HandoffRequest
    ) -> dict[str, object]:
        run = _handle_store_error(
            lambda: orchestrator.handoff(
                run_id, request.branch, request.base_branch, request.body
            )
        )
        return _run_dict(run)

    @app.post("/runs/{run_id}/fail")
    def fail(  # pyright: ignore[reportUnusedFunction]
        run_id: str, request: FailureRequest
    ) -> dict[str, object]:
        return _run_dict(
            _handle_store_error(lambda: orchestrator.fail(run_id, request.error))
        )

    return app


def _handle_store_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except StoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _run_dict(run: RunState) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "repository": run.repository,
        "pbi_number": run.pbi_number,
        "title": run.title,
        "stage": run.stage.value,
        "status": run.status.value,
        "attempt": run.attempt,
        "branch": run.branch,
        "pull_request_url": run.pull_request_url,
        "last_error": run.last_error,
    }


app = create_app()
