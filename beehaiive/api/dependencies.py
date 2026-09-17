import os
import secrets
from typing import Any

from fastapi import Header, HTTPException, Request

from beehaiive.agent import worker_secret_values
from beehaiive.api.helpers.http import _required_header as _required_header
from beehaiive.models import RunStatus
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import RoutingError
from beehaiive.storage import StoreError
from beehaiive.workflow import WorkflowRole, WorkflowService


def build_route_dependencies(
    runtime: dict[str, Any],
    api_key: str | None,
    review_actor: str | None,
    workflow_actor: WorkflowRole | str | None,
) -> dict[str, Any]:
    orchestrator = runtime["orchestrator"]
    review_repair_service = runtime["review_repair_service"]
    workflow_service = runtime["workflow_service"]
    review_operations_enabled = runtime["review_operations_enabled"]
    configured_projects = runtime["configured_projects"]
    configured_api_key = (
        api_key if api_key is not None else os.environ.get("BEEHAIIVE_API_KEY")
    )
    refinement_secret_values = (configured_api_key,) if configured_api_key else ()
    worker_secret_values_from_executor = tuple(
        value
        for value in getattr(
            getattr(runtime["agent_worker"], "executor", None),
            "_secret_values",
            (),
        )
        if isinstance(value, str) and value
    )
    dashboard_secret_values = tuple(
        dict.fromkeys(
            value
            for value in (
                *refinement_secret_values,
                *worker_secret_values_from_executor,
                *worker_secret_values(),
            )
            if value
        )
    )
    configured_review_actor = (
        review_actor
        if review_actor is not None
        else os.environ.get("BEEHAIIVE_REVIEW_ACTOR")
    )
    configured_workflow_actor = (
        workflow_actor
        if workflow_actor is not None
        else os.environ.get("BEEHAIIVE_WORKFLOW_ACTOR")
    )
    refinement_path = (
        "/projects/{project_id}/repositories/{repository:path}/pbis/"
        "{pbi_number}/refinement"
    )

    def require_api_key(supplied_api_key: str | None) -> None:
        if not configured_api_key:
            raise HTTPException(
                status_code=503,
                detail="Mutation authorization is not configured",
            )
        if supplied_api_key is None or not secrets.compare_digest(
            supplied_api_key, configured_api_key
        ):
            raise HTTPException(status_code=401, detail="Invalid API key")

    def require_review_access(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        if not review_operations_enabled:
            raise HTTPException(
                status_code=503, detail="Review operations are disabled in demo mode"
            )
        require_api_key(supplied_api_key)
        if configured_review_actor is None or not configured_review_actor.strip():
            raise HTTPException(
                status_code=503, detail="Review actor is not configured"
            )
        return configured_review_actor

    def require_review_repair_service() -> ReviewRepairService:
        if review_repair_service is None:
            raise HTTPException(
                status_code=503,
                detail="Review repair dispatch is not configured",
            )
        return review_repair_service

    def require_routing_run_access(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
        lease_token: str | None = Header(default=None, alias="X-Lease-Token"),
    ) -> None:
        require_api_key(supplied_api_key)
        run_id = request.path_params.get("run_id")
        if not isinstance(run_id, str):
            raise HTTPException(status_code=403, detail="Run is not authorized")
        run = orchestrator.store.get_run(run_id)
        if (
            run is None
            or run.project_id not in configured_projects
            or not orchestrator.store.is_active_repository(
                run.project_id, run.repository
            )
        ):
            raise HTTPException(status_code=403, detail="Run is not authorized")
        if run.status is RunStatus.ACTIVE:
            try:
                orchestrator.store.validate_lease(
                    run_id, _required_header(lease_token, "X-Lease-Token")
                )
            except StoreError as exc:
                raise HTTPException(
                    status_code=403, detail="Run is not authorized"
                ) from exc

    def require_mutation_access(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        if supplied_api_key is None:
            dashboard_marker = request.headers.get("X-BeeHAIve-Dashboard")
            origin = request.headers.get("Origin")
            expected_origin = str(request.base_url).rstrip("/")
            fetch_site = request.headers.get("Sec-Fetch-Site")
            if (
                dashboard_marker != "1"
                or (origin is None or origin.rstrip("/") != expected_origin)
                or fetch_site not in {None, "same-origin", "same-site", "none"}
            ):
                require_api_key(None)
            require_api_key(configured_api_key)
        else:
            require_api_key(supplied_api_key)

        path_params = request.path_params
        project_id = path_params.get("project_id")
        repository = path_params.get("repository")
        run_id = path_params.get("run_id")
        if isinstance(run_id, str):
            run = orchestrator.store.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=403, detail="Run is not authorized")
            project_id = run.project_id
            repository = run.repository
        if not isinstance(project_id, str) or project_id not in configured_projects:
            raise HTTPException(status_code=403, detail="Project is not authorized")
        if isinstance(repository, str) and not orchestrator.store.is_active_repository(
            project_id, repository
        ):
            raise HTTPException(status_code=403, detail="Repository is not authorized")

    def require_workflow_access(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        require_api_key(supplied_api_key)

    def require_workflow_service() -> WorkflowService:
        if workflow_service is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow coordination is not configured",
            )
        return workflow_service

    def require_workflow_operator(
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        require_api_key(supplied_api_key)
        if configured_workflow_actor is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow operator identity is not configured",
            )
        try:
            actor = WorkflowRole(configured_workflow_actor)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail="Workflow operator identity is not configured",
            ) from exc
        if actor is not WorkflowRole.OPERATOR:
            raise HTTPException(
                status_code=403,
                detail="Workflow operator approval is required",
            )
        return actor.value

    def require_refinement_operator(
        request: Request,
        supplied_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        require_mutation_access(request, supplied_api_key)
        return require_workflow_operator(supplied_api_key)

    def require_handoff_lease_token(
        handoff_id: str,
        supplied_lease_token: str | None,
    ) -> None:
        service = require_workflow_service()
        handoff = service.get_handoff(handoff_id)
        service.store.require_lease_token(handoff.lease_id, supplied_lease_token)

    def require_project_access(request: Request) -> None:
        project_id = request.path_params.get("project_id")
        if not isinstance(project_id, str) or project_id not in configured_projects:
            raise HTTPException(status_code=403, detail="Project is not authorized")

    def routing_snapshot(run_id: str, *, required: bool) -> dict[str, object] | None:
        router = orchestrator.model_router
        assert router is not None
        if not required and router.store.get_problem(run_id) is None:
            return None
        try:
            return router.snapshot(run_id).as_dict()
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    return {
        "refinement_path": refinement_path,
        "refinement_secret_values": refinement_secret_values,
        "dashboard_secret_values": dashboard_secret_values,
        "routing_snapshot": routing_snapshot,
        "require_api_key": require_api_key,
        "require_handoff_lease_token": require_handoff_lease_token,
        "require_mutation_access": require_mutation_access,
        "require_project_access": require_project_access,
        "require_refinement_operator": require_refinement_operator,
        "require_review_access": require_review_access,
        "require_review_repair_service": require_review_repair_service,
        "require_routing_run_access": require_routing_run_access,
        "require_workflow_access": require_workflow_access,
        "require_workflow_operator": require_workflow_operator,
        "require_workflow_service": require_workflow_service,
    }
