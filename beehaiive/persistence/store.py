from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .actions import ActionsMixin
from .agent_sessions import AgentSessionsMixin
from .budget_evidence import BudgetEvidenceMixin
from .canonical_lifecycle import CanonicalLifecycleMixin
from .execution_lease import ExecutionLeaseMixin
from .graph_definitions import GraphDefinitionMixin
from .graph_safety import GraphSafetyMixin
from .graph_transitions import GraphTransitionMixin
from .handoff_mutation import HandoffMutationMixin
from .handoff_record import HandoffRecordMixin
from .idea_capture import IdeaCaptureMixin
from .lease_store import LeaseStoreMixin
from .meta_review import MetaReviewMixin
from .migrations import StorageMigrationMixin
from .operator_notification_delivery import OperatorNotificationDeliveryMixin
from .operator_question_answers import OperatorQuestionAnswersMixin
from .operator_questions import OperatorQuestionsMixin
from .pbi_creation import PbiCreationMixin
from .pbi_refinement_answer import PbiRefinementAnswerMixin
from .pbi_refinement_reopen import PbiRefinementReopenMixin
from .pbi_refinement_start import PbiRefinementStartMixin
from .project_claim import ProjectClaimMixin
from .project_read import ProjectReadMixin
from .project_sync import ProjectSyncMixin
from .routing_failure import RoutingFailureMixin
from .row_mapping import RowMappingMixin
from .run_completion import RunCompletionMixin
from .run_failure import RunFailureMixin
from .run_transition import RunTransitionMixin
from .runtime_settings import RuntimeSettingsMixin
from .schema import StorageSchemaMixin
from .task_contract import TaskContractMixin


class OrchestratorStore(
    StorageSchemaMixin,
    StorageMigrationMixin,
    PbiRefinementStartMixin,
    PbiRefinementAnswerMixin,
    PbiRefinementReopenMixin,
    ProjectSyncMixin,
    ProjectClaimMixin,
    RunTransitionMixin,
    RuntimeSettingsMixin,
    HandoffRecordMixin,
    LeaseStoreMixin,
    ExecutionLeaseMixin,
    TaskContractMixin,
    OperatorQuestionAnswersMixin,
    OperatorQuestionsMixin,
    OperatorNotificationDeliveryMixin,
    RunFailureMixin,
    RunCompletionMixin,
    RoutingFailureMixin,
    ActionsMixin,
    IdeaCaptureMixin,
    PbiCreationMixin,
    HandoffMutationMixin,
    AgentSessionsMixin,
    GraphDefinitionMixin,
    GraphSafetyMixin,
    GraphTransitionMixin,
    BudgetEvidenceMixin,
    CanonicalLifecycleMixin,
    MetaReviewMixin,
    ProjectReadMixin,
    RowMappingMixin,
):
    def __init__(
        self, database: str | Path = ":memory:", lease_seconds: int = 300
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = lease_seconds
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize(self) -> None:
        with self._lock:
            self._initialize_schema()
            self._migrate_schema()


__all__ = ["OrchestratorStore"]
