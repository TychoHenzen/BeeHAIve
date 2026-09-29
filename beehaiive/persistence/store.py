from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from beehaiive.sqlite_store_core import SQLiteStoreCoreMixin

from .actions import ActionsMixin
from .admission import AdmissionMixin
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
from .station_issues import StationIssueMixin
from .task_contract import TaskContractMixin
from .worker_hosts import WorkerHostsMixin


class OrchestratorStore(
    SQLiteStoreCoreMixin,
    AdmissionMixin,
    StorageSchemaMixin,
    StorageMigrationMixin,
    StationIssueMixin,
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
    WorkerHostsMixin,
):
    def __init__(
        self,
        database: str | Path = ":memory:",
        lease_seconds: int = 300,
        *,
        admission_capacity: int | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = lease_seconds
        if admission_capacity is not None and (
            type(admission_capacity) is not int or admission_capacity <= 0
        ):
            raise ValueError("admission_capacity must be a positive integer")
        self._admission_capacity = admission_capacity
        SQLiteStoreCoreMixin.__init__(self, database, self._initialize_store)

    def _initialize_store(self) -> None:
        self._initialize()
        self._initialize_admission()
        if self._admission_capacity is not None:
            self.recover_expired_admissions()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._sqlite_transaction(
            before_yield=self._check_admission_configuration
        ) as connection:
            yield connection

    def _initialize(self) -> None:
        with self._lock:
            self._initialize_schema()
            self._migrate_schema()


__all__ = ["OrchestratorStore"]
