from collections.abc import Collection, Mapping
from typing import Any

from fastapi import FastAPI

from beehaiive import Orchestrator, OrchestratorStore
from beehaiive.agent import AgentWorkerManager
from beehaiive.api.dependencies import build_route_dependencies
from beehaiive.api.lifecycle import (
    register_background_handlers,
    register_error_handlers,
)
from beehaiive.api.routes.graph_safety import (
    register_routes as register_graph_safety_routes,
)
from beehaiive.api.routes.pbi_creation import (
    register_routes as register_pbi_creation_routes,
)
from beehaiive.api.routes.pbi_refinement import (
    register_routes as register_pbi_refinement_routes,
)
from beehaiive.api.routes.project_dashboard import (
    register_routes as register_project_dashboard_routes,
)
from beehaiive.api.routes.reviews import register_routes as register_reviews_routes
from beehaiive.api.routes.runs import register_routes as register_runs_routes
from beehaiive.api.routes.system import register_routes as register_system_routes
from beehaiive.api.routes.workflow import register_routes as register_workflow_routes
from beehaiive.api.runtime import build_api_runtime
from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.graph_safety import GraphSafetyService
from beehaiive.meta_review import MetaReviewService
from beehaiive.review import (
    PullRequestReviewProvider,
    ReviewAuthorizer,
    ReviewConcern,
    ReviewReader,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import ModelExecutor, ModelRouter, RoutingStore
from beehaiive.workflow import WorkflowRole, WorkflowService


def create_app(
    store: OrchestratorStore | None = None,
    orchestrator: Orchestrator | None = None,
    api_key: str | None = None,
    allowed_project_ids: Collection[str] | None = None,
    review_store: ReviewStore | None = None,
    review_service: ReviewService | None = None,
    review_provider: PullRequestReviewProvider | None = None,
    review_readers: Mapping[ReviewConcern, ReviewReader] | None = None,
    review_authorizer: ReviewAuthorizer | None = None,
    review_actor: str | None = None,
    require_review_adapters: bool = False,
    routing_store: RoutingStore | None = None,
    model_router: ModelRouter | None = None,
    model_executor: ModelExecutor | None = None,
    agent_worker: AgentWorkerManager | None = None,
    meta_review_service: MetaReviewService | None = None,
    workflow_service: WorkflowService | None = None,
    workflow_actor: WorkflowRole | str | None = None,
    graph_safety_service: GraphSafetyService | None = None,
    conflict_repair_service: ConflictRepairService | None = None,
    review_repair_service: ReviewRepairService | None = None,
) -> FastAPI:
    runtime_options: dict[str, Any] = {
        "agent_worker": agent_worker,
        "allowed_project_ids": allowed_project_ids,
        "conflict_repair_service": conflict_repair_service,
        "meta_review_service": meta_review_service,
        "model_executor": model_executor,
        "model_router": model_router,
        "orchestrator": orchestrator,
        "require_review_adapters": require_review_adapters,
        "review_authorizer": review_authorizer,
        "review_provider": review_provider,
        "review_readers": review_readers,
        "review_repair_service": review_repair_service,
        "review_service": review_service,
        "review_store": review_store,
        "routing_store": routing_store,
        "store": store,
        "workflow_service": workflow_service,
        "graph_safety_service": graph_safety_service,
    }
    runtime = build_api_runtime(runtime_options)
    app = FastAPI(title="BeeHAIve")
    register_error_handlers(app)
    register_background_handlers(app, runtime)
    dependencies = build_route_dependencies(
        runtime, api_key, review_actor, workflow_actor
    )
    route_context = runtime | dependencies
    register_reviews_routes(app, route_context)
    register_workflow_routes(app, route_context)
    register_graph_safety_routes(app, route_context)
    register_system_routes(app, route_context)
    register_pbi_refinement_routes(app, route_context)
    register_pbi_creation_routes(app, route_context)
    register_project_dashboard_routes(app, route_context)
    register_runs_routes(app, route_context)
    return app


__all__ = ["create_app"]
