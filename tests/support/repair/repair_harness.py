from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beehaiive.review import (
    ReviewService,
    ReviewStore,
)
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import RoutingStore
from beehaiive.workflow import (
    WorkflowService,
    WorkflowStore,
)
from tests.support.repair.commit_agent import CommitAgent
from tests.support.repair.fixture_provider import FixtureProvider


@dataclass
class RepairHarness:
    repository: Path
    remote: Path
    source_head: str
    review_store: ReviewStore
    workflow_store: WorkflowStore
    routing_store: RoutingStore
    reviews: ReviewService
    workflow: WorkflowService
    provider: FixtureProvider
    agent: CommitAgent
    service: ReviewRepairService
    cycle_id: str
    selected_id: str
    unselected_id: str

    def close(self) -> None:
        self.review_store.close()
        self.workflow_store.close()
        self.routing_store.close()
