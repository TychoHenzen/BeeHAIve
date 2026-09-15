from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any, cast

from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.models import HandoffIntent, RunStatus, Stage
from beehaiive.routing import AttemptOutcome, RoutingStatus
from beehaiive.storage import MAX_AGENT_DIAGNOSTIC_LENGTH, StoreError
from beehaiive.workflow import GateResult, GitDeliveryStatus, LeaseStatus, WorkflowError

from .worker_text import _gate_summary as _gate_summary
from .worker_text import redact_worker_text as redact_worker_text


class WorkerRunMixin:
    def _run(
        self: Any, run_id: str, lease_token: str, model_override: str | None = None
    ) -> None:
        workflow_service = self.workflow_service
        with self._lock:
            workspace_lease = self._workspace_leases.get(run_id)
            validate_workspace_lease = self._workspace_validators.get(run_id)
        heartbeat_stop = Event()
        heartbeat_errors: list[Exception] = []
        heartbeat_thread: Thread | None = None

        if workspace_lease is not None and workflow_service is not None:

            def heartbeat() -> None:
                heartbeat_seconds = workflow_service.store.lease_heartbeat_seconds
                while not heartbeat_stop.wait(heartbeat_seconds):
                    try:
                        workflow_service.store.renew_lease(
                            workspace_lease.lease_id, workspace_lease.lease_token
                        )
                    except Exception as exc:
                        heartbeat_errors.append(exc)
                        self.cancel(run_id)
                        return

            heartbeat_thread = Thread(
                target=heartbeat, name=f"beehaiive-lease-{run_id[:8]}", daemon=True
            )
            heartbeat_thread.start()
        preserve_workspace = False
        preflight_gate: GateResult | None = None
        try:
            pending_lookup = getattr(self.orchestrator.store, "pending_handoff", None)
            pending_handoff = (
                cast(HandoffIntent | None, pending_lookup(run_id, lease_token))
                if callable(pending_lookup)
                else None
            )
            if pending_handoff is not None:
                self.orchestrator.handoff(
                    run_id,
                    pending_handoff.branch,
                    pending_handoff.base_branch,
                    pending_handoff.body,
                    lease_token,
                    head_sha=pending_handoff.head_sha,
                    verification_evidence=pending_handoff.verification_evidence,
                )
                return
            if workspace_lease is not None and workflow_service is not None:
                preflight_gate = workflow_service.before_model_call(
                    workspace_lease.lease_id
                )
                assert preflight_gate is not None
                if not preflight_gate.allowed:
                    failure = json.dumps(
                        {"quality_gate": _gate_summary(preflight_gate)},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    self.orchestrator.store.fail_agent_run(
                        run_id,
                        redact_worker_text(
                            failure, max_length=MAX_AGENT_DIAGNOSTIC_LENGTH
                        ),
                        lease_token,
                        claimable=False,
                    )
                    return
            self.orchestrator.advance(run_id, Stage.IMPLEMENT, lease_token)
            if validate_workspace_lease is not None:
                validate_workspace_lease()
            if model_override is None:
                routing = self.orchestrator.run_implementation_attempt(
                    run_id, lease_token
                )
            else:
                routing = self.orchestrator.run_implementation_attempt(
                    run_id, lease_token, model_override=model_override
                )
            heartbeat_stop.set()
            if heartbeat_thread is not None and workflow_service is not None:
                heartbeat_thread.join(
                    timeout=max(workflow_service.store.lease_heartbeat_seconds, 1.0)
                )
                if heartbeat_thread.is_alive():
                    raise WorkflowError("Workflow lease heartbeat did not stop")
            if heartbeat_errors:
                raise WorkflowError(
                    f"Workflow workspace lease was lost: {heartbeat_errors[0]}"
                )
            if (
                validate_workspace_lease is not None
                and routing.state.status is not RoutingStatus.HUMAN_HANDOFF
            ):
                validate_workspace_lease()
            attempt = routing.attempt
            if routing.state.status is RoutingStatus.HUMAN_HANDOFF:
                return
            elif (
                attempt is not None
                and attempt.outcome is AttemptOutcome.SUCCESS
                and routing.state.status is RoutingStatus.RESOLVED
            ):
                try:
                    assert workspace_lease is not None and workflow_service is not None
                    workflow_service.retain_workspace(
                        workspace_lease.lease_id, workspace_lease.lease_token
                    )
                    preserve_workspace = True
                    delivery = self.commit_and_push(run_id)
                    task_result = getattr(routing, "task_result", None)
                    if delivery.status is not GitDeliveryStatus.PUSHED:
                        result = self._delivery_summary(
                            routing.execution_result
                            or "Bounded agent completed the demo",
                            delivery,
                        )
                        self.orchestrator.store.complete_agent_run(
                            run_id, result, lease_token
                        )
                        return
                    if not delivery.commit_sha:
                        raise StoreError(
                            "A verified pushed commit is required before "
                            "pull-request handoff"
                        )
                    if (
                        not isinstance(task_result, TaskResult)
                        or task_result.outcome is not TaskOutcome.PASS
                    ):
                        raise StoreError(
                            "A passing structured task result is required before "
                            "pull-request handoff"
                        )
                    result = self._delivery_summary(
                        routing.execution_result or "Bounded agent completed the demo",
                        delivery,
                    )
                    secret_values = tuple(
                        value
                        for value in getattr(self.executor, "_secret_values", ())
                        if isinstance(value, str)
                    )
                    verification_evidence = redact_worker_text(
                        json.dumps(
                            {
                                "task_result": task_result.as_dict(),
                                "git_delivery": delivery.as_dict(),
                                "quality_gates": {
                                    key: _gate_summary(gate)
                                    for key, gate in (
                                        (
                                            "before_model_call",
                                            preflight_gate,
                                        ),
                                        (
                                            "git_delivery",
                                            workflow_service.store.latest_gate(
                                                workspace_lease.lease_id,
                                                "git_delivery",
                                            ),
                                        ),
                                    )
                                    if gate is not None
                                },
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        secret_values,
                        max_length=MAX_AGENT_DIAGNOSTIC_LENGTH,
                    )
                    self.orchestrator.handoff(
                        run_id,
                        delivery.branch,
                        None,
                        result,
                        lease_token,
                        head_sha=delivery.commit_sha,
                        verification_evidence=verification_evidence,
                    )
                except Exception as exc:
                    reopen = getattr(self.orchestrator, "recover_routing_problem", None)
                    if callable(reopen):
                        reopen(run_id, f"Run result persistence failed: {exc}")
                    raise
            else:
                failure = (
                    (attempt.failure_context if attempt is not None else "")
                    or routing.decision.failure_context
                    or ("Bounded agent did not complete the demo")
                )
                self.orchestrator.store.fail_agent_run(
                    run_id, redact_worker_text(failure), lease_token
                )
        except Exception as exc:
            run = self.orchestrator.store.get_run(run_id)
            if (
                run is not None
                and run.status is RunStatus.ACTIVE
                and run.lease_token == lease_token
            ):
                failure = redact_worker_text(f"Agent worker failed: {exc}")
                try:
                    self.orchestrator.store.fail_agent_run(run_id, failure, lease_token)
                except StoreError:
                    recover = getattr(
                        self.orchestrator.store, "fail_agent_run_after_lease_loss", None
                    )
                    if callable(recover):
                        recover(run_id, failure, expected_lease_token=lease_token)
        finally:
            heartbeat_stop.set()
            cleanup_error: WorkflowError | None = None
            if (
                heartbeat_thread is not None
                and workflow_service is not None
                and heartbeat_thread.is_alive()
            ):
                heartbeat_thread.join(
                    timeout=max(workflow_service.store.lease_heartbeat_seconds, 1.0)
                )
            if (
                workspace_lease is not None
                and workflow_service is not None
                and not preserve_workspace
            ):
                worktree_path = Path(workspace_lease.worktree_path)
                dirty = False
                if worktree_path.is_dir():
                    try:
                        dirty = not workflow_service.worktrees.clean(worktree_path)
                    except WorkflowError:
                        dirty = True
                if dirty:
                    try:
                        current = workflow_service.store.get_lease(
                            workspace_lease.lease_id
                        )
                        if current is not None and current.status is LeaseStatus.ACTIVE:
                            workflow_service.retain_workspace(
                                workspace_lease.lease_id,
                                workspace_lease.lease_token,
                            )
                    except WorkflowError:
                        pass
                    preserve_workspace = True
                else:
                    try:
                        workflow_service.discard_workspace(
                            workspace_lease.lease_id,
                            "Dashboard worker did not complete",
                        )
                    except WorkflowError as exc:
                        cleanup_error = exc
            self.executor.release_run(run_id)
            with self._lock:
                self._workspace_leases.pop(run_id, None)
                self._workspace_validators.pop(run_id, None)
                current = self._threads.get(run_id)
                if current is not None and current is current_thread():
                    self._threads.pop(run_id, None)
            if (
                workspace_lease is not None
                and workflow_service is not None
                and not preserve_workspace
                and cleanup_error is not None
            ):
                raise StoreError(f"Worker workspace cleanup failed: {cleanup_error}")


__all__ = ["WorkerRunMixin"]
