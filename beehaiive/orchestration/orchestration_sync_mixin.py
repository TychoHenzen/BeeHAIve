from __future__ import annotations

from typing import Any

from ..checks import blocking_check_failure
from ..models import (
    ProjectSnapshot,
    RunState,
    Stage,
)


class OrchestrationSyncMixin:
    def synchronize(
        self: Any, project_id: str, *, force_refresh: bool = False
    ) -> dict[str, object]:
        if force_refresh:
            invalidate = getattr(self.provider, "invalidate_discovery_cache", None)
            if callable(invalidate):
                invalidate()
        snapshot = self.provider.discover_project(project_id)
        self._cancel_removed_workers(snapshot)
        self.store.sync_project(snapshot)
        self._route_blocking_check_failures(snapshot)
        return self.store.project_state(project_id)

    def claim(
        self: Any,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
        *,
        expected_run_id: str | None = None,
        agent_session: tuple[str, str] | None = None,
    ) -> RunState | None:
        return self.store.claim_next(
            project_id,
            repository,
            owner_id,
            lease_token,
            expected_run_id=expected_run_id,
            agent_session=agent_session,
        )

    def advance(self: Any, run_id: str, target: Stage, lease_token: str) -> RunState:
        run = self.store.advance(run_id, target, lease_token)
        if target is Stage.IMPLEMENT:
            self._ensure_routing_problem(run.run_id)
        return run

    def renew_lease(self: Any, run_id: str, lease_token: str) -> RunState:
        return self.store.renew_lease(run_id, lease_token)

    def _route_blocking_check_failures(self: Any, snapshot: ProjectSnapshot) -> None:
        active_runs = {
            (run.repository, run.pbi_number): run
            for run in self.store.active_runs_for_project(snapshot.project_id)
            if run.stage is Stage.IMPLEMENT and run.lease_token is not None
        }
        for repository in snapshot.repositories:
            for pbi in repository.pbis:
                failure = blocking_check_failure(pbi.metadata)
                run = active_runs.get((repository.name, pbi.number))
                if failure is None or run is None or run.lease_token is None:
                    continue
                self.fail(run.run_id, failure, run.lease_token)
