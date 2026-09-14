from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast
from uuid import uuid4

from beehaiive.storage import OrchestratorStore, StoreError

from .helpers import _incomplete_result, _request_fingerprint
from .pbi_creation_conflict_error import PbiCreationConflictError
from .pbi_creation_error import PbiCreationError
from .pbi_creation_progress import PbiCreationProgress
from .pbi_creation_provider import PbiCreationProvider
from .pbi_creation_request import PbiCreationRequest
from .pbi_creation_validation_error import PbiCreationValidationError

__all__ = ["PbiCreationService"]


class PbiCreationService:
    """Coordinate durable idempotency state with the GitHub provider."""

    def __init__(self, store: OrchestratorStore, provider: PbiCreationProvider) -> None:
        self._store = store
        self._provider = provider

    def create(
        self, request: PbiCreationRequest, idempotency_key: str
    ) -> dict[str, object]:
        request.validate()
        if (
            not idempotency_key
            or len(idempotency_key) > 200
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in idempotency_key
            )
        ):
            raise PbiCreationValidationError(
                "Idempotency-Key must contain 1 to 200 printable ASCII characters",
                code="invalid_idempotency_key",
            )

        key_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        request_hash = _request_fingerprint(request)
        lease_owner = str(uuid4())
        attempt, claimed = self._store.begin_pbi_creation(
            request.project_id,
            key_hash,
            request_hash,
            request.repository,
            lease_owner,
        )
        if attempt.get("request_hash") != request_hash:
            raise PbiCreationConflictError()
        if attempt.get("status") == "complete":
            result = attempt.get("result")
            if isinstance(result, Mapping):
                return dict(cast(Mapping[str, object], result))
            raise StoreError("Completed PBI creation has no result")
        if not claimed:
            return _incomplete_result(attempt)

        progress = PbiCreationProgress.from_record(attempt)
        try:
            target = self._provider.prepare_pbi_creation(request)
        except PbiCreationError as exc:
            self._store.fail_pbi_creation(
                request.project_id,
                key_hash,
                lease_owner,
                "incomplete",
                "preflight",
                exc.code,
                type(exc).__name__,
            )
            raise
        except Exception as exc:
            self._store.fail_pbi_creation(
                request.project_id,
                key_hash,
                lease_owner,
                "incomplete",
                "preflight",
                "provider_error",
                type(exc).__name__,
            )
            raise PbiCreationError(
                "GitHub creation preflight failed",
                code="provider_error",
                status_code=502,
            ) from exc

        def save_progress(updated: PbiCreationProgress) -> None:
            nonlocal progress
            self._store.checkpoint_pbi_creation(
                request.project_id,
                key_hash,
                lease_owner,
                updated.as_dict(),
            )
            progress = updated

        try:
            result = self._provider.create_pbi(request, target, progress, save_progress)
            serialized = result.as_dict(request.repository)
            self._store.finish_pbi_creation(
                request.project_id, key_hash, lease_owner, serialized
            )
            return serialized
        except Exception as exc:
            unknown_outcome = (
                progress.issue_create_started and progress.issue_id is None
            )
            if isinstance(exc, PbiCreationError):
                unknown_outcome = unknown_outcome or exc.unknown_outcome
            status = "outcome_unknown" if unknown_outcome else "incomplete"
            failure_code = (
                "outcome_unknown"
                if unknown_outcome
                else exc.code
                if isinstance(exc, PbiCreationError)
                else "provider_error"
            )
            self._store.fail_pbi_creation(
                request.project_id,
                key_hash,
                lease_owner,
                status,
                progress.current_step or "reconcile",
                failure_code,
                type(exc).__name__,
            )
            failed_attempt = self._store.get_pbi_creation(request.project_id, key_hash)
            if failed_attempt is None:
                raise StoreError("PBI creation attempt disappeared") from exc
            return _incomplete_result(failed_attempt)
