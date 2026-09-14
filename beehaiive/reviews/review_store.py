from __future__ import annotations

from .review_store_core_mixin import ReviewStoreCoreMixin
from .review_store_cycle_mixin import ReviewStoreCycleMixin
from .review_store_publication_mixin import ReviewStorePublicationMixin
from .review_store_repair_create_mixin import ReviewStoreRepairCreateMixin
from .review_store_repair_transition_mixin import ReviewStoreRepairTransitionMixin
from .review_store_row_mixin import ReviewStoreRowMixin


class ReviewStore(
    ReviewStoreCoreMixin,
    ReviewStoreRowMixin,
    ReviewStoreCycleMixin,
    ReviewStoreRepairCreateMixin,
    ReviewStoreRepairTransitionMixin,
    ReviewStorePublicationMixin,
):
    pass
