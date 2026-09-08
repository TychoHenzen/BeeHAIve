"""Application service coordinating provider work and durable state."""

from __future__ import annotations

from threading import Lock

from .models import HandoffRequest, HandoffResult, RunState, RunStatus, Stage
from .provider import ProjectProvider
from .storage import OrchestratorStore, StoreError


class Orchestrator:
    """Coordinates discovery, repository writer claims, and handoffs."""

    def __init__(self, store: OrchestratorStore, provider: ProjectProvider) -> None:
        self.store = store
        self.provider = provider
        self._handoff_locks: dict[str, Lock] = {}
        self._handoff_locks_guard = Lock()

    def synchronize(self, project_id: str) -> dict[str, object]:
        snapshot = self.provider.discover_project(project_id)
        self.store.sync_project(snapshot)
        return self.store.project_state(project_id)

    def claim(self, project_id: str, repository: str) -> RunState | None:
        return self.store.claim_next(project_id, repository)

    def advance(self, run_id: str, target: Stage) -> RunState:
        return self.store.advance(run_id, target)

    def handoff(
        self,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
    ) -> RunState:
        with self._handoff_lock(run_id):
            run = self.store.get_run(run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            if run.status is RunStatus.COMPLETED:
                return run
            if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            pending = self.store.pending_handoff(run_id)
            if pending is not None:
                if (
                    pending.branch != branch
                    or pending.body != body
                    or (base_branch is not None and pending.base_branch != base_branch)
                ):
                    raise StoreError(
                        "Handoff request does not match the persisted intent"
                    )
                intent = self.store.prepare_handoff(
                    run_id,
                    pending.branch,
                    pending.base_branch,
                    pending.body,
                )
            else:
                resolved_base_branch = self.provider.resolve_base_branch(
                    run.repository, base_branch
                )
                intent = self.store.prepare_handoff(
                    run_id, branch, resolved_base_branch, body
                )
            if intent.run.status is RunStatus.COMPLETED:
                return intent.run
            result = self.provider.create_handoff(
                HandoffRequest(
                    project_id=intent.run.project_id,
                    repository=intent.run.repository,
                    pbi_number=intent.run.pbi_number,
                    title=intent.run.title,
                    branch=intent.branch,
                    base_branch=intent.base_branch,
                    body=intent.body,
                )
            )
            return self.store.record_handoff(
                run_id,
                result.branch,
                result.pull_request_url,
                result.pull_request_number,
            )

    def fail(self, run_id: str, error: str) -> RunState:
        return self.store.fail(run_id, error)

    def _handoff_lock(self, run_id: str) -> Lock:
        with self._handoff_locks_guard:
            lock = self._handoff_locks.get(run_id)
            if lock is None:
                lock = Lock()
                self._handoff_locks[run_id] = lock
            return lock


__all__ = ["HandoffResult", "Orchestrator"]
