from collections.abc import Mapping

from .workflow_role import WorkflowRole

DEFAULT_LEASE_TTL_SECONDS = 300
ALLOWED_ROLE_TRANSITIONS: Mapping[WorkflowRole, frozenset[WorkflowRole]] = {
    WorkflowRole.PLANNER: frozenset({WorkflowRole.WRITER}),
    WorkflowRole.WRITER: frozenset({WorkflowRole.REVIEWER, WorkflowRole.OPERATOR}),
    WorkflowRole.REVIEWER: frozenset({WorkflowRole.OPERATOR}),
    WorkflowRole.OPERATOR: frozenset(),
}
