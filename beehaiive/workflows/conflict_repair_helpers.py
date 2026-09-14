from __future__ import annotations

from ..agent import redact_worker_text
from ..models import PullRequestSnapshot
from ..routing import ModelExecution


def same_repair_identity(
    expected: PullRequestSnapshot, current: PullRequestSnapshot
) -> bool:
    return (
        expected.pull_request_id == current.pull_request_id
        and expected.repository == current.repository
        and expected.source_branch == current.source_branch
        and expected.source_head == current.source_head
        and expected.target_branch == current.target_branch
        and expected.target_head == current.target_head
        and current.conflict_state == "conflicting"
    )


def execution_evidence(execution: ModelExecution) -> dict[str, object]:
    return {
        "outcome": str(execution.outcome),
        "input_tokens": execution.input_tokens,
        "output_tokens": execution.output_tokens,
        "result": redact_worker_text(execution.result),
        "failure_context": redact_worker_text(execution.failure_context),
    }
