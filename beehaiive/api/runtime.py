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
from beehaiive.autonomous import AutonomousLifecycleService
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
from beehaiive.storage import (
    DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
    DEFAULT_WORKER_HOST_STALE_SECONDS,
)


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
    autonomous_service = options.get("autonomous_service")
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
                database if database == ":memory:" else Path(database),
                admission_capacity=(
                    int(os.environ.get("BEEHAIIVE_ADMISSION_CAPACITY", "1"))
                    if os.environ.get("BEEHAIIVE_ADMISSION_ENABLED", "").lower()
                    in {"1", "true"}
                    else None
                ),
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
    persisted_settings = orchestrator.store.get_runtime_settings()
    environment_scheduler_config = SchedulerConfig.from_environment()
    persisted_scheduler = persisted_settings.get("scheduler")
    if isinstance(persisted_scheduler, dict):
        persisted_scheduler = cast(dict[str, object], persisted_scheduler)
        enabled = persisted_scheduler.get("enabled")
        poll_interval = persisted_scheduler.get("poll_interval_seconds")
        max_concurrency = persisted_scheduler.get("max_concurrency")
        try:
            scheduler_config = SchedulerConfig(
                enabled=(
                    enabled
                    if type(enabled) is bool
                    else environment_scheduler_config.enabled
                ),
                poll_interval_seconds=(
                    float(poll_interval)
                    if isinstance(poll_interval, (int, float))
                    else environment_scheduler_config.poll_interval_seconds
                ),
                max_concurrency=(
                    max_concurrency
                    if type(max_concurrency) is int
                    else environment_scheduler_config.max_concurrency
                ),
            )
        except (TypeError, ValueError):
            scheduler_config = environment_scheduler_config
    else:
        scheduler_config = environment_scheduler_config
    if autonomous_service is None:
        autonomous_service = AutonomousLifecycleService(
            orchestrator,
            max_concurrency=scheduler_config.max_concurrency,
            workflow_service=workflow_service,
        )
    else:
        set_capacity = getattr(autonomous_service, "set_max_concurrency", None)
        if callable(set_capacity):
            set_capacity(scheduler_config.max_concurrency)
    worker_host_heartbeat_seconds = _worker_host_seconds(
        "BEEHAIIVE_WORKER_HEARTBEAT_SECONDS",
        DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
    )
    worker_host_stale_seconds = _worker_host_seconds(
        "BEEHAIIVE_WORKER_STALE_SECONDS",
        DEFAULT_WORKER_HOST_STALE_SECONDS,
    )
    configure_worker_hosts = getattr(
        orchestrator.store, "configure_worker_host_liveness", None
    )
    if callable(configure_worker_hosts):
        configure_worker_hosts(worker_host_heartbeat_seconds, worker_host_stale_seconds)
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
                host_id=os.environ.get("BEEHAIIVE_WORKER_HOST_ID"),
                worker_slots=_worker_host_slots(scheduler_config.max_concurrency),
                capabilities=_worker_host_capabilities(),
                heartbeat_seconds=worker_host_heartbeat_seconds,
                stale_seconds=worker_host_stale_seconds,
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

    project_boundary = frozenset(_configured_project_ids(allowed_project_ids))
    configured_projects = set(project_boundary)
    persisted_projects = persisted_settings.get("projects")
    if isinstance(persisted_projects, list):
        persisted_projects = cast(list[object], persisted_projects)
        valid_projects = {
            value.strip()
            for value in persisted_projects
            if isinstance(value, str)
            and value.count(":") == 1
            and value.rsplit(":", 1)[1].isdigit()
            and value.strip()
        }
        if valid_projects and valid_projects.issubset(project_boundary):
            configured_projects = valid_projects
    recover_pending = getattr(autonomous_service, "recover_pending", None)
    if callable(recover_pending):
        recover_pending(configured_projects)
    scheduler = None
    if scheduler_config.enabled and agent_worker is None:
        raise ValueError("The scheduler requires a configured dashboard agent worker")
    if scheduler_config.enabled and not configured_projects:
        raise ValueError("The scheduler requires at least one allowlisted project")
    if (
        agent_worker is not None
        and getattr(agent_worker, "workflow_service", None) is not None
        and configured_projects
    ):
        scheduler = AgentScheduler(
            orchestrator,
            agent_worker,
            configured_projects,
            scheduler_config,
            autonomous_start=autonomous_service.start,
            autonomous_has_capacity=(
                autonomous_service.has_capacity
                if callable(getattr(autonomous_service, "has_capacity", None))
                else None
            ),
            autonomous_set_capacity=(
                autonomous_service.set_max_concurrency
                if callable(getattr(autonomous_service, "set_max_concurrency", None))
                else None
            ),
            autonomous_active_count=(
                autonomous_service.active_count
                if callable(getattr(autonomous_service, "active_count", None))
                else None
            ),
            allow_disabled=not scheduler_config.enabled,
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
        "project_boundary": project_boundary,
        "runtime_settings": persisted_settings,
        "scheduler_config": scheduler_config,
        "scheduler": scheduler,
        "workflow_service": workflow_service,
        "graph_safety_service": graph_safety_service,
        "autonomous_service": autonomous_service,
        "require_review_adapters": require_review_adapters,
    }


def _worker_host_slots(default: int) -> int:
    raw = os.environ.get("BEEHAIIVE_WORKER_SLOTS", "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("BEEHAIIVE_WORKER_SLOTS must be an integer") from exc


def _worker_host_capabilities() -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.environ.get("BEEHAIIVE_WORKER_CAPABILITIES", "").split(",")
        if value.strip()
    )


def _worker_host_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
