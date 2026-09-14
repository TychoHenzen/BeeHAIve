from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

WEBHOOK_URL_ENV = "BEEHAIIVE_NOTIFICATION_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET"
NOTIFICATION_LIMIT = 25
NOTIFICATION_TIMEOUT_SECONDS = 2.0


def dispatch_pending_operator_notifications(
    store: Any, *, limit: int = NOTIFICATION_LIMIT, run_id: str | None = None
) -> dict[str, int]:
    """Deliver a bounded batch of durable question notifications."""

    if limit < 1:
        raise ValueError("Notification batch limit must be positive")
    question_ids = store.pending_operator_notification_ids(limit, run_id=run_id)
    url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    secret = os.environ.get(WEBHOOK_SECRET_ENV, "")
    if not url or not secret:
        for question_id in question_ids:
            store.mark_operator_notification_not_configured(
                question_id, "Webhook URL and secret are not configured"
            )
        return {
            "attempted": 0,
            "delivered": 0,
            "failed": 0,
            "not_configured": len(question_ids),
        }
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        for question_id in question_ids:
            store.mark_operator_notification_not_configured(
                question_id, "Webhook URL must use HTTPS and omit embedded credentials"
            )
        return {
            "attempted": 0,
            "delivered": 0,
            "failed": 0,
            "not_configured": len(question_ids),
        }

    results = {"attempted": 0, "delivered": 0, "failed": 0, "not_configured": 0}
    for question_id in question_ids:
        while True:
            claimed = store.claim_operator_notification(question_id)
            if claimed is None:
                break
            question = claimed["question"]
            lease_token = str(claimed["lease_token"])
            attempt = int(claimed["attempt"])
            payload = {
                "event": str(question["kind"]),
                "question_id": str(question["question_id"]),
                "revision": int(question["revision"]),
                "project_id": str(question["project_id"]),
                "repository": str(question["repository"]),
                "pbi_number": int(question["pbi_number"]),
                "run_id": str(question["run_id"]),
                "question": str(question["question"]),
                "evidence": question["evidence"],
                "created_at": str(question["created_at"]),
            }
            results["attempted"] += 1
            status_code: int | None = None
            delivered = False
            retryable = False
            error: str | None = None
            retry_after: float | None = None
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Idempotency-Key": str(question["question_id"]),
                    },
                    timeout=NOTIFICATION_TIMEOUT_SECONDS,
                )
                status_code = response.status_code
                delivered = 200 <= status_code < 300
                retryable = status_code == 429 or status_code >= 500
                if not delivered:
                    error = f"Webhook returned HTTP {status_code}"
                    raw_retry_after = response.headers.get("Retry-After")
                    if raw_retry_after is not None:
                        try:
                            retry_after = max(0.0, min(float(raw_retry_after), 1.0))
                        except ValueError:
                            retry_after = None
            except httpx.RequestError as exc:
                retryable = True
                error = f"Webhook request failed: {type(exc).__name__}"

            final_status = store.complete_operator_notification_attempt(
                question_id,
                lease_token,
                status_code=status_code,
                delivered=delivered,
                retryable=retryable,
                error=error,
            )
            if delivered:
                results["delivered"] += 1
                break
            if final_status == "pending":
                time.sleep(
                    retry_after
                    if retry_after is not None
                    else min(0.1 * (2 ** (attempt - 1)), 0.5)
                )
                continue
            if final_status == "failed":
                results["failed"] += 1
            break
    return results


__all__ = [
    "WEBHOOK_URL_ENV",
    "WEBHOOK_SECRET_ENV",
    "dispatch_pending_operator_notifications",
]
