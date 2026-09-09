"""Application service coordinating provider work and durable state."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock

from .models import HandoffRequest, RunState, RunStatus, Stage
from .provider import ProjectProvider
from .storage import OrchestratorStore, StoreError


class _KeyedLockEntry:
    def __init__(self) -> None:
        self.lock = Lock()
        self.users = 0


class _KeyedLockManager:
    def __init__(self) -> None:
        self._entries: dict[str, _KeyedLockEntry] = {}
        self._guard = Lock()

    @contextmanager
    def acquire(self, key: str) -> Generator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _KeyedLockEntry()
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]


class Orchestrator:
    """Coordinates discovery, repository writer claims, and handoffs."""

    def __init__(self, store: OrchestratorStore, provider: ProjectProvider) -> None:
        self.store = store
        self.provider = provider
        self._handoff_locks = _KeyedLockManager()

    def synchronize(self, project_id: str) -> dict[str, object]:
        snapshot = self.provider.discover_project(project_id)
        self.store.sync_project(snapshot)
        return self.store.project_state(project_id)

    def claim(
        self,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
    ) -> RunState | None:
        return self.store.claim_next(project_id, repository, owner_id, lease_token)

    def advance(self, run_id: str, target: Stage, lease_token: str) -> RunState:
        return self.store.advance(run_id, target, lease_token)

    def renew_lease(self, run_id: str, lease_token: str) -> RunState:
        return self.store.renew_lease(run_id, lease_token)

    def handoff(
        self,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
    ) -> RunState:
        with self._handoff_locks.acquire(run_id):
            run = self.store.get_run(run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            if run.status is RunStatus.COMPLETED:
                self.store.renew_lease(run_id, lease_token)
                return run
            if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            pending = self.store.pending_handoff(run_id, lease_token)
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
                    lease_token,
                )
            else:
                resolved_base_branch = self.provider.validate_handoff(
                    run.repository, branch, base_branch
                )
                intent = self.store.prepare_handoff(
                    run_id, branch, resolved_base_branch, body, lease_token
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
                    run_id=intent.run.run_id,
                )
            )
            return self.store.record_handoff(
                run_id,
                result.branch,
                result.pull_request_url,
                result.pull_request_number,
                lease_token,
            )

    def fail(self, run_id: str, error: str, lease_token: str) -> RunState:
        return self.store.fail(run_id, error, lease_token)
