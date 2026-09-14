"""Deterministic adapters that make the local dashboard demo self-contained."""

from __future__ import annotations

from .review import REQUIRED_CONCERNS, ReviewConcern
from .reviews.demo_review_provider import DemoReviewProvider
from .reviews.demo_review_reader import DemoReviewReader


def demo_review_adapters() -> tuple[
    DemoReviewProvider, dict[ReviewConcern, DemoReviewReader]
]:
    reader = DemoReviewReader()
    return DemoReviewProvider(), {concern: reader for concern in REQUIRED_CONCERNS}
