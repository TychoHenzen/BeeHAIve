from __future__ import annotations

from .workflow_store_core_mixin import WorkflowStoreCoreMixin
from .workflow_store_gate_mixin import WorkflowStoreGateMixin
from .workflow_store_handoff_mixin import WorkflowStoreHandoffMixin
from .workflow_store_lease_read_mixin import WorkflowStoreLeaseReadMixin
from .workflow_store_lease_write_mixin import WorkflowStoreLeaseWriteMixin
from .workflow_store_record_mixin import WorkflowStoreRecordMixin
from .workflow_store_repair_mixin import WorkflowStoreRepairMixin


class WorkflowStore(
    WorkflowStoreCoreMixin,
    WorkflowStoreRecordMixin,
    WorkflowStoreRepairMixin,
    WorkflowStoreLeaseReadMixin,
    WorkflowStoreLeaseWriteMixin,
    WorkflowStoreHandoffMixin,
    WorkflowStoreGateMixin,
):
    pass
