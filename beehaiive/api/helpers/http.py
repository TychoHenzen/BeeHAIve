import sqlite3
from collections.abc import Callable

from fastapi import HTTPException

from beehaiive.meta_review import MetaReviewError
from beehaiive.pbi_creation import PbiCreationError
from beehaiive.persistence.admission import AdmissionError
from beehaiive.provider import ProviderError
from beehaiive.review import ReviewAdapterError, ReviewError
from beehaiive.storage import StoreError
from beehaiive.workflow import WorkflowError


def _pbi_refinement_failure(
    code: str, pending_step: str, message: str
) -> dict[str, object]:
    return {
        "status": "partial",
        "completed_steps": [],
        "pending_step": pending_step,
        "failure_code": code,
        "message": message,
    }


def _required_header(value: str | None, name: str) -> str:
    if not value or not value.strip():
        raise HTTPException(status_code=401, detail=f"{name} is required")
    return value


def _handle_store_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except AdmissionError as exc:
        status = 503 if exc.reason == "admission_authority_unavailable" else 409
        raise HTTPException(status_code=status, detail={"reason": exc.reason}) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503, detail={"reason": "admission_authority_unavailable"}
        ) from exc
    except StoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _handle_meta_review_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except PbiCreationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (MetaReviewError, StoreError):
        raise HTTPException(
            status_code=409, detail="Meta-review request could not be completed"
        ) from None


def _handle_review_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except ReviewAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _handle_workflow_error[T](function: Callable[[], T]) -> T:
    try:
        return function()
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = [
    "_pbi_refinement_failure",
    "_required_header",
    "_handle_store_error",
    "_handle_meta_review_error",
    "_handle_review_error",
    "_handle_workflow_error",
]
