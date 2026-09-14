from .review_concern import ReviewConcern

MAX_REVIEW_EVIDENCE_BYTES = 1_000_000


MAX_REVIEW_EVIDENCE_REFS = 100


MAX_REVIEW_REPAIR_FINDINGS = 20


REQUIRED_CONCERNS = (
    ReviewConcern.SECURITY,
    ReviewConcern.TEST_COVERAGE,
    ReviewConcern.CLEAN_CODE,
    ReviewConcern.PERFORMANCE,
)
