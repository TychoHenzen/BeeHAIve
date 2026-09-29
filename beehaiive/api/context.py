from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from beehaiive import Orchestrator, OrchestratorStore
from beehaiive.agent import AgentWorkerManager
from beehaiive.agent_stations import AgentStationService
from beehaiive.autonomous import AutonomousLifecycleService
from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.graph_safety import GraphSafetyService
from beehaiive.meta_review import MetaReviewService
from beehaiive.pbi_creation import PbiCreationService
from beehaiive.pbi_relations import PbiRelationService
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
from beehaiive.scheduler import AgentScheduler, SchedulerConfig
from beehaiive.workflow import WorkflowService

type RequireApiKey = Callable[[str | None], None]
type RequireHandoffLeaseToken = Callable[[str, str | None], None]
type RequireMutationAccess = Callable[[Request, str | None], None]
type RequireProjectAccess = Callable[[Request], None]
type RequireRefinementOperator = Callable[[Request, str | None], str]
type RequireReviewAccess = Callable[[str | None], str]
type RequireReviewRepairService = Callable[[], ReviewRepairService]
type RequireRoutingRunAccess = Callable[[Request, str | None, str | None], None]
type RequireWorkflowAccess = Callable[[str | None], None]
type RequireWorkflowOperator = Callable[[str | None], str]
type RequireDashboardWorkflowOperator = Callable[[Request, str | None], str]
type RequireWorkflowService = Callable[[], WorkflowService]


class RoutingSnapshot(Protocol):
    def __call__(self, run_id: str, *, required: bool) -> dict[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class ApiRuntimeOptions:
    agent_worker: AgentWorkerManager | None = None
    allowed_project_ids: Collection[str] | None = None
    conflict_repair_service: ConflictRepairService | None = None
    meta_review_service: MetaReviewService | None = None
    model_executor: ModelExecutor | None = None
    model_router: ModelRouter | None = None
    orchestrator: Orchestrator | None = None
    require_review_adapters: bool = False
    review_authorizer: ReviewAuthorizer | None = None
    review_provider: PullRequestReviewProvider | None = None
    review_readers: Mapping[ReviewConcern, ReviewReader] | None = None
    review_repair_service: ReviewRepairService | None = None
    review_service: ReviewService | None = None
    review_store: ReviewStore | None = None
    routing_store: RoutingStore | None = None
    store: OrchestratorStore | None = None
    workflow_service: WorkflowService | None = None
    graph_safety_service: GraphSafetyService | None = None
    autonomous_service: AutonomousLifecycleService | None = None
    agent_station_service: AgentStationService | None = None


@dataclass(frozen=True, slots=True)
class ApiRuntime:
    orchestrator: Orchestrator
    routing_service: ModelRouter
    pbi_creation_service: PbiCreationService
    pbi_relations_service: PbiRelationService
    conflict_repair_service: ConflictRepairService | None
    meta_review_service: MetaReviewService
    effective_executor: ModelExecutor | None
    agent_worker: AgentWorkerManager | None
    review_service: ReviewService
    review_repair_service: ReviewRepairService | None
    configured_projects: set[str]
    project_boundary: frozenset[str]
    runtime_settings: dict[str, object]
    scheduler_config: SchedulerConfig
    scheduler: AgentScheduler | None
    workflow_service: WorkflowService | None
    graph_safety_service: GraphSafetyService
    agent_station_service: AgentStationService
    autonomous_service: AutonomousLifecycleService
    require_review_adapters: bool


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    refinement_path: str
    refinement_secret_values: tuple[str, ...]
    dashboard_secret_values: tuple[str, ...]
    routing_snapshot: RoutingSnapshot
    require_api_key: RequireApiKey
    require_handoff_lease_token: RequireHandoffLeaseToken
    require_mutation_access: RequireMutationAccess
    require_dashboard_settings_mutation: RequireMutationAccess
    require_project_access: RequireProjectAccess
    require_refinement_operator: RequireRefinementOperator
    require_review_access: RequireReviewAccess
    require_review_repair_service: RequireReviewRepairService
    require_routing_run_access: RequireRoutingRunAccess
    require_workflow_access: RequireWorkflowAccess
    require_workflow_operator: RequireWorkflowOperator
    require_dashboard_workflow_operator: RequireDashboardWorkflowOperator
    require_workflow_service: RequireWorkflowService


@dataclass(frozen=True, slots=True)
class ApiRouteContext:
    orchestrator: Orchestrator
    routing_service: ModelRouter
    pbi_creation_service: PbiCreationService
    pbi_relations_service: PbiRelationService
    conflict_repair_service: ConflictRepairService | None
    meta_review_service: MetaReviewService
    agent_worker: AgentWorkerManager | None
    review_service: ReviewService
    review_repair_service: ReviewRepairService | None
    configured_projects: set[str]
    project_boundary: frozenset[str]
    runtime_settings: dict[str, object]
    scheduler_config: SchedulerConfig
    scheduler: AgentScheduler | None
    workflow_service: WorkflowService | None
    graph_safety_service: GraphSafetyService
    agent_station_service: AgentStationService
    autonomous_service: AutonomousLifecycleService
    refinement_path: str
    refinement_secret_values: tuple[str, ...]
    dashboard_secret_values: tuple[str, ...]
    routing_snapshot: RoutingSnapshot
    require_api_key: RequireApiKey
    require_handoff_lease_token: RequireHandoffLeaseToken
    require_mutation_access: RequireMutationAccess
    require_dashboard_settings_mutation: RequireMutationAccess
    require_project_access: RequireProjectAccess
    require_refinement_operator: RequireRefinementOperator
    require_review_access: RequireReviewAccess
    require_review_repair_service: RequireReviewRepairService
    require_routing_run_access: RequireRoutingRunAccess
    require_workflow_access: RequireWorkflowAccess
    require_workflow_operator: RequireWorkflowOperator
    require_dashboard_workflow_operator: RequireDashboardWorkflowOperator
    require_workflow_service: RequireWorkflowService

    @classmethod
    def from_parts(
        cls, runtime: ApiRuntime, dependencies: ApiDependencies
    ) -> ApiRouteContext:
        return cls(
            orchestrator=runtime.orchestrator,
            routing_service=runtime.routing_service,
            pbi_creation_service=runtime.pbi_creation_service,
            pbi_relations_service=runtime.pbi_relations_service,
            conflict_repair_service=runtime.conflict_repair_service,
            meta_review_service=runtime.meta_review_service,
            agent_worker=runtime.agent_worker,
            review_service=runtime.review_service,
            review_repair_service=runtime.review_repair_service,
            configured_projects=runtime.configured_projects,
            project_boundary=runtime.project_boundary,
            runtime_settings=runtime.runtime_settings,
            scheduler_config=runtime.scheduler_config,
            scheduler=runtime.scheduler,
            workflow_service=runtime.workflow_service,
            graph_safety_service=runtime.graph_safety_service,
            agent_station_service=runtime.agent_station_service,
            autonomous_service=runtime.autonomous_service,
            refinement_path=dependencies.refinement_path,
            refinement_secret_values=dependencies.refinement_secret_values,
            dashboard_secret_values=dependencies.dashboard_secret_values,
            routing_snapshot=dependencies.routing_snapshot,
            require_api_key=dependencies.require_api_key,
            require_handoff_lease_token=dependencies.require_handoff_lease_token,
            require_mutation_access=dependencies.require_mutation_access,
            require_dashboard_settings_mutation=dependencies.require_dashboard_settings_mutation,
            require_project_access=dependencies.require_project_access,
            require_refinement_operator=dependencies.require_refinement_operator,
            require_review_access=dependencies.require_review_access,
            require_review_repair_service=dependencies.require_review_repair_service,
            require_routing_run_access=dependencies.require_routing_run_access,
            require_workflow_access=dependencies.require_workflow_access,
            require_workflow_operator=dependencies.require_workflow_operator,
            require_dashboard_workflow_operator=dependencies.require_dashboard_workflow_operator,
            require_workflow_service=dependencies.require_workflow_service,
        )


__all__ = [
    "ApiDependencies",
    "ApiRouteContext",
    "ApiRuntime",
    "ApiRuntimeOptions",
    "RoutingSnapshot",
]
