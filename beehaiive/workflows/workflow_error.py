from __future__ import annotations


class WorkflowError(RuntimeError):
    """Raised when a workflow cannot safely advance."""
