"""Idempotent creation of a GitHub issue in the configured Project."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

from .storage import OrchestratorStore, StoreError


class PbiCreationError(ValueError):
    """A safe, bounded error returned by the PBI creation API."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 422,
        unknown_outcome: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.unknown_outcome = unknown_outcome


class PbiCreationConflictError(PbiCreationError):
    """Raised when a key is reused with a different request."""

    def __init__(self) -> None:
        super().__init__(
            "Idempotency-Key conflicts with an earlier request",
            code="idempotency_key_conflict",
            status_code=409,
        )


class PbiCreationScopeError(PbiCreationError):
    """Raised when a request targets a project or repository outside scope."""

    def __init__(
        self, message: str = "Project or repository is not authorized"
    ) -> None:
        super().__init__(message, code="scope_rejected", status_code=403)


class PbiCreationValidationError(PbiCreationError):
    """Raised when the request or its GitHub targets fail validation."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message, code=code, status_code=422)


@dataclass(frozen=True, slots=True)
class PbiCreationRequest:
    project_id: str
    repository: str
    title: str
    body: str
    labels: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.project_id or self.project_id != self.project_id.strip():
            raise PbiCreationValidationError("Project id is required")
        if (
            not self.repository
            or self.repository != self.repository.strip()
            or self.repository.count("/") != 1
        ):
            raise PbiCreationValidationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if not self.title.strip() or len(self.title) > 256:
            raise PbiCreationValidationError(
                "Title must contain 1 to 256 characters", code="invalid_title"
            )
        if not self.body.strip() or len(self.body) > 65_536:
            raise PbiCreationValidationError(
                "Body must contain 1 to 65536 characters", code="invalid_body"
            )
        if len(self.labels) > 20:
            raise PbiCreationValidationError(
                "At most 20 labels may be supplied", code="invalid_labels"
            )
        if any(
            not label or label != label.strip() or len(label) > 100
            for label in self.labels
        ):
            raise PbiCreationValidationError(
                "Labels must contain 1 to 100 non-whitespace characters",
                code="invalid_labels",
            )
        if len(set(self.labels)) != len(self.labels):
            raise PbiCreationValidationError(
                "Labels must not contain duplicates", code="invalid_labels"
            )


@dataclass(frozen=True, slots=True)
class PbiCreationTarget:
    project_node_id: str
    repository_node_id: str
    status_field_id: str
    backlog_option_id: str
    backlog_status: str
    label_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PbiCreationProgress:
    issue_create_started: bool = False
    issue_id: str | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    project_item_id: str | None = None
    completed_steps: tuple[str, ...] = ()
    current_step: str | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> PbiCreationProgress:
        raw_steps: object = record.get("completed_steps", [])
        steps: tuple[str, ...] = ()
        if isinstance(raw_steps, list):
            steps = tuple(
                step for step in cast(list[object], raw_steps) if isinstance(step, str)
            )
        raw_issue_id: object = record.get("issue_id")
        raw_issue_url: object = record.get("issue_url")
        raw_project_item_id: object = record.get("project_item_id")
        raw_current_step: object = record.get("current_step")
        issue_number = record.get("issue_number")
        return cls(
            issue_create_started=record.get("issue_create_started") is True,
            issue_id=raw_issue_id if isinstance(raw_issue_id, str) else None,
            issue_number=issue_number if type(issue_number) is int else None,
            issue_url=raw_issue_url if isinstance(raw_issue_url, str) else None,
            project_item_id=raw_project_item_id
            if isinstance(raw_project_item_id, str)
            else None,
            completed_steps=steps,
            current_step=raw_current_step
            if isinstance(raw_current_step, str)
            else None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "issue_create_started": self.issue_create_started,
            "issue_id": self.issue_id,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "project_item_id": self.project_item_id,
            "completed_steps": list(self.completed_steps),
            "current_step": self.current_step,
        }


@dataclass(frozen=True, slots=True)
class PbiCreationResult:
    issue_id: str
    issue_number: int
    issue_url: str
    labels: tuple[str, ...]
    project_item_id: str
    project_status: str
    completed_steps: tuple[str, ...]

    def as_dict(self, repository: str) -> dict[str, object]:
        return {
            "status": "complete",
            "repository": repository,
            "issue": {
                "id": self.issue_id,
                "number": self.issue_number,
                "url": self.issue_url,
            },
            "labels": list(self.labels),
            "project": {
                "item_id": self.project_item_id,
                "status": self.project_status,
            },
            "completed_steps": list(self.completed_steps),
        }


class PbiCreationProvider(Protocol):
    """GitHub operations required to create and reconcile one PBI."""

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        """Validate live project, repository, labels, and Backlog option."""

        ...

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        """Create or reconcile an issue and confirm its Project state."""

        ...


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


def _request_fingerprint(request: PbiCreationRequest) -> str:
    payload = json.dumps(
        {
            "project_id": request.project_id,
            "repository": request.repository,
            "title": request.title,
            "body": request.body,
            "labels": list(request.labels),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _incomplete_result(record: Mapping[str, object]) -> dict[str, object]:
    issue_number = record.get("issue_number")
    issue_url = record.get("issue_url")
    issue: dict[str, object] | None = None
    if type(issue_number) is int and isinstance(issue_url, str):
        issue = {
            "id": record.get("issue_id"),
            "number": issue_number,
            "url": issue_url,
        }
    status = record.get("status")
    result: dict[str, object] = {
        "status": status if isinstance(status, str) else "incomplete",
        "issue": issue,
        "completed_steps": record.get("completed_steps", []),
        "failed_step": record.get("failed_step") or record.get("current_step"),
        "operator_action_required": status == "outcome_unknown",
    }
    failure_code = record.get("failure_code")
    failure_class = record.get("failure_class")
    if isinstance(failure_code, str):
        result["failure"] = {
            "code": failure_code[:80],
            "type": failure_class[:80]
            if isinstance(failure_class, str)
            else "ProviderError",
        }
    return result
