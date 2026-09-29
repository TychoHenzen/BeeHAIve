from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException

from beehaiive.contract_types.validation import _redact_text
from beehaiive.service_failures import FailureCategory


class ServiceError(Protocol):
    code: str


@dataclass(frozen=True, slots=True)
class ServiceErrorPolicy:
    unavailable_categories: frozenset[str] = frozenset()
    unavailable_codes: frozenset[str] = frozenset()
    conflict_codes: frozenset[str] = frozenset()
    invalid_codes: frozenset[str] = frozenset()
    default_status: int = 422
    classified_detail: bool = True
    redact_message: bool = True
    default_category: FailureCategory | str | None = None


def service_error_http_exception(
    error: ServiceError, policy: ServiceErrorPolicy
) -> HTTPException:
    category = getattr(error, "category", None)
    category_value = _category_value(category)
    if error.code == "not_found":
        status = 404
    elif error.code in policy.unavailable_codes or (
        category_value is not None and category_value in policy.unavailable_categories
    ):
        status = 503
    elif error.code in policy.conflict_codes:
        status = 409
    elif error.code in policy.invalid_codes:
        status = 422
    else:
        status = policy.default_status

    if not policy.classified_detail:
        return HTTPException(status_code=status, detail=str(error))

    detail_category = category if category is not None else policy.default_category
    message = str(error)
    if policy.redact_message:
        message = _redact_text(message, 512)
    return HTTPException(
        status_code=status,
        detail={
            "code": error.code,
            "failure_class": _category_value(detail_category),
            "message": message,
        },
    )


def _category_value(category: object) -> str | None:
    if category is None:
        return None
    if isinstance(category, FailureCategory):
        return category.value
    return str(category)


__all__ = [
    "ServiceErrorPolicy",
    "service_error_http_exception",
]
