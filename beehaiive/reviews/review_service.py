from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .allow_list_review_authorizer import AllowListReviewAuthorizer
from .review_concern import ReviewConcern
from .review_service_access_mixin import ReviewServiceAccessMixin
from .review_service_cycle_run_mixin import ReviewServiceCycleRunMixin
from .review_service_cycle_storage_mixin import ReviewServiceCycleStorageMixin
from .review_service_finding_mutation_mixin import ReviewServiceFindingMutationMixin
from .review_service_finding_record_mixin import ReviewServiceFindingRecordMixin
from .review_service_publication_mixin import ReviewServicePublicationMixin
from .review_service_repair_transition_mixin import ReviewServiceRepairTransitionMixin
from .review_service_status_mixin import ReviewServiceStatusMixin

if TYPE_CHECKING:
    from .pull_request_review_provider import PullRequestReviewProvider
    from .review_authorizer import ReviewAuthorizer
    from .review_reader import ReviewReader
    from .review_store import ReviewStore


class ReviewService(
    ReviewServiceAccessMixin,
    ReviewServiceRepairTransitionMixin,
    ReviewServiceCycleRunMixin,
    ReviewServiceCycleStorageMixin,
    ReviewServiceFindingRecordMixin,
    ReviewServiceFindingMutationMixin,
    ReviewServicePublicationMixin,
    ReviewServiceStatusMixin,
):
    def __init__(
        self,
        store: ReviewStore,
        provider: PullRequestReviewProvider | None = None,
        readers: Mapping[ReviewConcern, ReviewReader] | None = None,
        authorizer: ReviewAuthorizer | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.readers = dict(readers or {})
        self.authorizer = authorizer or AllowListReviewAuthorizer()
