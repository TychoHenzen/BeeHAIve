import os
from pathlib import Path
from typing import Any, cast

from beehaiive import EnvironmentGitHubProvider, Orchestrator, OrchestratorStore
from beehaiive.agent import (
    AgentWorkerManager,
    CancellableModelExecutor,
    CodexExecModelExecutor,
)
from beehaiive.api.helpers.configuration import (
    _configured_project_ids as _configured_project_ids,
)
from beehaiive.api.helpers.configuration import (
    _routing_config_from_environment as _routing_config_from_environment,
)
from beehaiive.conflict_repair import ConflictRepairAgent, ConflictRepairService
from beehaiive.graph_safety import GraphSafetyService
from beehaiive.meta_review import MetaReviewService
from beehaiive.pbi_creation import PbiCreationProvider, PbiCreationService
from beehaiive.pbi_relations import PbiRelationProvider, PbiRelationService
from beehaiive.review import ReviewService, ReviewStore
from beehaiive.review_github import GitHubReviewProvider, github_review_readers
from beehaiive.review_repair import ReviewRepairService, SelectedRepairAgent
from beehaiive.routing import ModelRouter, RoutingStore
from beehaiive.scheduler import AgentScheduler, SchedulerConfig


def build_api_runtime(options: dict[str, Any]) -> dict[str, Any]:
    agent_worker = options["agent_worker"]
    allowed_project_ids = options["allowed_project_ids"]
    conflict_repair_service = options["conflict_repair_service"]
    meta_review_service = options["meta_review_service"]
    model_executor = options["model_executor"]
    model_router = options["model_router"]
    orchestrator = options["orchestrator"]
    require_review_adapters = options["require_review_adapters"]
    review_authorizer = options["review_authorizer"]
    review_provider = options["review_provider"]
    review_readers = options["review_readers"]
    review_repair_service = options["review_repair_service"]
    review_service = options["review_service"]
    review_store = options["review_store"]
    routing_store = options["routing_store"]
    store = options["store"]
    workflow_service = options["workflow_service"]
    graph_safety_service = options["graph_safety_service"]
    owns_orchestrator = orchestrator is None
    if orchestrator is not None and orchestrator.model_router is not None:
        if model_router is not None and model_router is not orchestrator.model_router:
            raise ValueError("The orchestrator and API must share one model router")
        if (
            routing_store is not None
            and routing_store is not orchestrator.model_router.store
        ):
            raise ValueError("The orchestrator and API must share one routing store")
        routing_service = orchestrator.model_router
    else:
        if (
            model_router is not None
            and routing_store is not None
            and model_router.store is not routing_store
        ):
            raise ValueError("The model router and API must share one routing store")
        if model_router is None:
            routing_database = os.environ.get(
                "BEEHAIIVE_ROUTING_DB", ".beehaiive/routing.db"
            )
            model_router = ModelRouter(
                routing_store or RoutingStore(routing_database),
                _routing_config_from_environment(),
            )
        routing_service = model_router

    if orchestrator is None:
        if store is None:
            database = os.environ.get("BEEHAIIVE_STATE_DB", ".beehaiive/state.db")
            store = OrchestratorStore(
                database if database == ":memory:" else Path(database)
            )
        if model_executor is None:
            model_executor = CodexExecModelExecutor.from_environment()
        orchestrator = Orchestrator(
            store, EnvironmentGitHubProvider(), routing_service, model_executor
        )
    else:
        orchestrator.model_router = routing_service
    if orchestrator.model_executor is None:
        orchestrator.model_executor = model_executor
    if (
        graph_safety_service is not None
        and graph_safety_service.store is not orchestrator.store
    ):
        raise ValueError("The graph safety service and API must share one state store")
    if graph_safety_service is None:
        graph_safety_service = GraphSafetyService(orchestrator.store)
    pbi_creation_service = PbiCreationService(
        orchestrator.store,
        cast(PbiCreationProvider, orchestrator.provider),
    )
    pbi_relations_service = PbiRelationService(
        cast(PbiRelationProvider, orchestrator.provider)
    )
    if (
        conflict_repair_service is None
        and workflow_service is not None
        and orchestrator.model_executor is not None
        and callable(getattr(orchestrator.provider, "get_pull_request", None))
        and callable(getattr(orchestrator.provider, "update_source_branch", None))
        and callable(getattr(orchestrator.model_executor, "execute_repair", None))
    ):
        conflict_repair_service = ConflictRepairService(
            workflow_service,
            orchestrator.provider,
            cast(ConflictRepairAgent, orchestrator.model_executor),
        )
    if (
        meta_review_service is not None
        and meta_review_service.store is not orchestrator.store
    ):
        raise ValueError("The meta-review service and API must share one state store")
    if (
        meta_review_service is not None
        and meta_review_service.routing_store is not routing_service.store
    ):
        raise ValueError("The meta-review service and API must share one routing store")
    if meta_review_service is None:
        meta_review_service = MetaReviewService(
            orchestrator.store, routing_service.store
        )
    effective_executor = orchestrator.model_executor
    if agent_worker is None and effective_executor is not None:
        required_worker_methods = (
            "cancel",
            "prepare_run",
            "release_run",
        )
        if not all(
            callable(getattr(effective_executor, name, None))
            for name in required_worker_methods
        ):
            if owns_orchestrator:
                raise ValueError(
                    "The model executor must support cancellation and "
                    "repository-bound worker preparation"
                )
        else:
            agent_worker = AgentWorkerManager(
                orchestrator,
                cast(CancellableModelExecutor, effective_executor),
                workflow_service,
            )
    if (
        review_service is not None
        and review_store is not None
        and review_service.store is not review_store
    ):
        raise ValueError("The review service and API must share one review store")
    if review_service is not None and any(
        value is not None
        for value in (review_provider, review_readers, review_authorizer)
    ):
        raise ValueError("Review adapters must be configured on the review service")
    review_operations_enabled = (
        os.environ.get("BEEHAIIVE_REVIEW_MODE", "").strip().lower() != "demo"
    )
    if (
        review_service is None
        and require_review_adapters
        and review_operations_enabled
        and review_provider is None
        and review_readers is None
    ):
        review_provider = GitHubReviewProvider()
        review_readers = github_review_readers()
    if review_service is None:
        review_database = os.environ.get("BEEHAIIVE_REVIEW_DB", ".beehaiive/reviews.db")
        review_service = ReviewService(
            review_store or ReviewStore(review_database),
            provider=review_provider,
            readers=review_readers,
            authorizer=review_authorizer,
        )
    if (
        review_repair_service is not None
        and review_repair_service.reviews is not review_service
    ):
        raise ValueError("The review repair service must share one review service")
    if (
        review_repair_service is None
        and review_operations_enabled
        and workflow_service is not None
        and review_service.provider is not None
        and callable(getattr(orchestrator.provider, "get_pull_request", None))
        and callable(getattr(orchestrator.provider, "update_source_branch", None))
        and callable(getattr(effective_executor, "execute_scoped_repair", None))
        and callable(getattr(effective_executor, "cancel", None))
    ):
        review_repair_service = ReviewRepairService(
            review_service,
            workflow_service,
            orchestrator.provider,
            routing_service,
            cast(SelectedRepairAgent, effective_executor),
        )

    configured_projects = _configured_project_ids(allowed_project_ids)
    scheduler_config = SchedulerConfig.from_environment()
    scheduler = None
    if scheduler_config.enabled:
        if agent_worker is None:
            raise ValueError(
                "The scheduler requires a configured dashboard agent worker"
            )
        scheduler = AgentScheduler(
            orchestrator, agent_worker, configured_projects, scheduler_config
        )
    return {
        "orchestrator": orchestrator,
        "routing_service": routing_service,
        "pbi_creation_service": pbi_creation_service,
        "pbi_relations_service": pbi_relations_service,
        "conflict_repair_service": conflict_repair_service,
        "meta_review_service": meta_review_service,
        "effective_executor": effective_executor,
        "agent_worker": agent_worker,
        "review_service": review_service,
        "review_repair_service": review_repair_service,
        "review_operations_enabled": review_operations_enabled,
        "configured_projects": configured_projects,
        "scheduler_config": scheduler_config,
        "scheduler": scheduler,
        "workflow_service": workflow_service,
        "graph_safety_service": graph_safety_service,
        "require_review_adapters": require_review_adapters,
    }
