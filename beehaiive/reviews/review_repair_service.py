from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from ..provider import ProjectProvider
from ..review import (
    PullRequestReviewProvider,
    ReviewService,
)
from ..routing import (
    ModelRouter,
)
from ..workflow import (
    WorkflowService,
)
from .review_repair_access_mixin import ReviewRepairAccessMixin
from .review_repair_delivery_mixin import ReviewRepairDeliveryMixin
from .review_repair_recovery_mixin import ReviewRepairRecoveryMixin
from .review_repair_run_mixin import ReviewRepairRunMixin
from .review_repair_validation_mixin import ReviewRepairValidationMixin

if TYPE_CHECKING:
    from .selected_repair_agent import SelectedRepairAgent


class ReviewRepairService(
    ReviewRepairAccessMixin,
    ReviewRepairRecoveryMixin,
    ReviewRepairRunMixin,
    ReviewRepairDeliveryMixin,
    ReviewRepairValidationMixin,
):
    def __init__(
        self,
        reviews: ReviewService,
        workflow: WorkflowService,
        provider: ProjectProvider,
        router: ModelRouter,
        agent: SelectedRepairAgent,
        worktree_root: str | Path | None = None,
    ) -> None:
        if reviews.provider is None:
            raise ValueError("A pull-request review provider is required")
        self.reviews = reviews
        self.review_provider: PullRequestReviewProvider = reviews.provider
        self.workflow = workflow
        self.provider = provider
        self.router = router
        self.agent = agent
        self.worktree_root = (
            Path(worktree_root).resolve()
            if worktree_root is not None
            else workflow.worktrees.repository
            / ".beehaiive"
            / "review-repair-worktrees"
        )
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._cancel_events: dict[str, Event] = {}
