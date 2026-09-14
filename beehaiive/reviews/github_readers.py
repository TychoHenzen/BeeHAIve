from __future__ import annotations

from ..review import (
    ReviewConcern,
)
from .github_evidence_review_reader import GitHubEvidenceReviewReader


def github_review_readers() -> dict[ReviewConcern, GitHubEvidenceReviewReader]:
    return {
        ReviewConcern.SECURITY: GitHubEvidenceReviewReader(ReviewConcern.SECURITY),
        ReviewConcern.TEST_COVERAGE: GitHubEvidenceReviewReader(
            ReviewConcern.TEST_COVERAGE
        ),
        ReviewConcern.CLEAN_CODE: GitHubEvidenceReviewReader(ReviewConcern.CLEAN_CODE),
        ReviewConcern.PERFORMANCE: GitHubEvidenceReviewReader(
            ReviewConcern.PERFORMANCE
        ),
    }
