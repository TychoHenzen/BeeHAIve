from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.handoff_helpers import (
    _begin_handoff_mutation as _begin_handoff_mutation,
)
from beehaiive.github.handoff_helpers import (
    _finish_handoff_mutation as _finish_handoff_mutation,
)
from beehaiive.models import HandoffRequest


class HandoffEnsureMixin:
    def _ensure_completion_step(
        self: Any,
        request: HandoffRequest,
        mutation: str,
        operation_key: str,
        target: Mapping[str, object],
        inspect_state: Callable[[], Mapping[str, object]],
        is_complete: Callable[[Mapping[str, object]], bool],
        can_apply: Callable[[Mapping[str, object]], bool],
        apply: Callable[[Mapping[str, object]], None],
    ) -> dict[str, object]:
        for _ in range(2):
            try:
                state = inspect_state()
            except ProviderError as exc:
                return {
                    "status": self._provider_failure_status(exc),
                    "error_class": type(exc).__name__,
                }
            if state.get("valid") is not True:
                return {
                    "status": "operator_required",
                    "reason": state.get("error", "Completion identity is unproven"),
                }

            action = self._handoff_action(request, mutation, operation_key)
            if is_complete(state):
                if action is None:
                    action_id = _begin_handoff_mutation(
                        request, mutation, operation_key, target
                    )
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                elif isinstance(action.get("id"), str):
                    _finish_handoff_mutation(
                        request,
                        cast(str, action["id"]),
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                return {"status": "completed", "reconciliation": "readback_present"}

            if not can_apply(state):
                return {
                    "status": "operator_required",
                    "reason": "Remote completion state changed unexpectedly",
                }

            if action is not None:
                action_target, action_result = self._audit_action_parts(action)
                action_status = action.get("status")
                request_record = action.get("request")
                attempt = (
                    cast(Mapping[str, object], request_record).get("attempt")
                    if isinstance(request_record, Mapping)
                    else None
                )
                if action_status in {"pending", "uncertain"}:
                    return {"status": "deferred", "reason": "prior_write_unresolved"}
                if action_status == "succeeded":
                    return {
                        "status": "operator_required",
                        "reason": "previous_write_readback_conflicts",
                    }
                if (
                    action_status != "failed"
                    or action_result.get("retryable") is not True
                ):
                    return {
                        "status": "operator_required",
                        "reason": "previous_write_not_retryable",
                    }
                retry_at = action_result.get("retry_after_at")
                if isinstance(retry_at, (int, float)) and time.time() < retry_at:
                    return {
                        "status": "deferred",
                        "retry_after_at": retry_at,
                    }
                if type(attempt) is int and attempt >= 2:
                    return {"status": "deferred", "reason": "retry_limit_reached"}
                if any(
                    action_target.get(key) != value for key, value in target.items()
                ):
                    return {
                        "status": "operator_required",
                        "reason": "prior_write_target_changed",
                    }

            action_id = _begin_handoff_mutation(
                request, mutation, operation_key, target
            )
            try:
                apply(state)
            except ProviderError as exc:
                try:
                    observed = inspect_state()
                except ProviderError as read_error:
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "uncertain",
                        {
                            "reconciliation": "readback_unavailable",
                            "error_class": type(read_error).__name__,
                        },
                    )
                    return {
                        "status": self._provider_failure_status(read_error),
                        "error_class": type(read_error).__name__,
                    }
                if observed.get("valid") is True and is_complete(observed):
                    _finish_handoff_mutation(
                        request,
                        action_id,
                        "succeeded",
                        {"reconciliation": "readback_present"},
                    )
                    return {"status": "completed", "reconciliation": "readback_present"}
                delay = self._retry_delay(exc)
                attempt = 1
                latest = self._handoff_action(request, mutation, operation_key)
                if latest is not None:
                    attempt_record = latest.get("request")
                    if isinstance(attempt_record, Mapping):
                        raw_attempt = cast(Mapping[str, object], attempt_record).get(
                            "attempt"
                        )
                        if type(raw_attempt) is int:
                            attempt = raw_attempt
                retry_at = time.time() + delay if delay is not None else None
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "failed",
                    {
                        "reconciliation": "readback_absent",
                        "error_class": type(exc).__name__,
                        "retryable": delay is not None,
                        **(
                            {"retry_after_at": retry_at} if retry_at is not None else {}
                        ),
                    },
                )
                if delay is not None and attempt == 1:
                    if delay > 1.0:
                        return {"status": "deferred", "retry_after_at": retry_at}
                    if delay > 0:
                        time.sleep(delay)
                    continue
                return {
                    "status": "deferred" if delay is not None else "operator_required",
                    "error_class": type(exc).__name__,
                    **({"reason": "retry_limit_reached"} if delay is not None else {}),
                }

            try:
                observed = inspect_state()
            except ProviderError as exc:
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "uncertain",
                    {
                        "reconciliation": "readback_unavailable",
                        "error_class": type(exc).__name__,
                    },
                )
                return {
                    "status": self._provider_failure_status(exc),
                    "error_class": type(exc).__name__,
                }
            if observed.get("valid") is True and is_complete(observed):
                _finish_handoff_mutation(
                    request,
                    action_id,
                    "succeeded",
                    {"reconciliation": "readback_present"},
                )
                return {"status": "completed", "reconciliation": "readback_present"}
            _finish_handoff_mutation(
                request,
                action_id,
                "uncertain",
                {"reconciliation": "write_not_confirmed"},
            )
            return {"status": "deferred", "reason": "write_not_confirmed"}
        return {"status": "deferred", "reason": "retry_limit_reached"}


__all__ = ["HandoffEnsureMixin"]
