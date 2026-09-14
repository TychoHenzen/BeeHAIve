from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from .finding_publication_state import FindingPublicationState
from .finding_status import FindingStatus
from .helpers import claim_is_active, current_timestamp, safe_publication_evidence
from .review_error import ReviewError

if TYPE_CHECKING:
    from .publication_outcome import PublicationOutcome
    from .review_finding import ReviewFinding
from typing import Any


class ReviewStorePublicationMixin:
    def claim_finding_publication(
        self: Any, finding_id: str
    ) -> tuple[str, ReviewFinding] | None:
        token = str(uuid4())
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if row is None:
                raise ReviewError(f"Unknown review finding: {finding_id}")
            if FindingStatus(str(row["status"])) is FindingStatus.RESOLVED:
                raise ReviewError("Resolved findings cannot be published")
            current = self.current_cycle_row(connection, str(row["pull_request_id"]))
            if (
                bool(row["stale"])
                or current is None
                or str(current["head_sha"]) != str(row["head_sha"])
            ):
                connection.execute(
                    """
                        UPDATE review_findings
                        SET stale = 1, publication_state = ?,
                            publication_claim_token = NULL,
                            publication_claim_expires_at = NULL, updated_at = ?
                        WHERE finding_id = ?
                        """,
                    (
                        FindingPublicationState.STALE.value,
                        current_timestamp(),
                        finding_id,
                    ),
                )
                return None
            state = FindingPublicationState(str(row["publication_state"]))
            if state in {
                FindingPublicationState.STALE,
                FindingPublicationState.DUPLICATE,
                FindingPublicationState.DUPLICATE_REMOTE,
            } or claim_is_active(row["publication_claim_expires_at"]):
                return None
            expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            connection.execute(
                """
                    UPDATE review_findings
                    SET publication_state = ?,
                        publication_attempts = publication_attempts + 1,
                        publication_claim_token = ?, publication_claim_expires_at = ?,
                        updated_at = ?
                    WHERE finding_id = ?
                    """,
                (
                    FindingPublicationState.PUBLISHING.value,
                    token,
                    expires_at,
                    current_timestamp(),
                    finding_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            assert updated is not None
            return token, self._finding_from_row(updated)

    def finish_finding_publication(
        self: Any,
        finding_id: str,
        claim_token: str,
        outcome: PublicationOutcome,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if row is None:
                raise ReviewError(f"Unknown review finding: {finding_id}")
            if row["publication_claim_token"] != claim_token:
                return False
            current = self.current_cycle_row(connection, str(row["pull_request_id"]))
            stale = (
                outcome.state is FindingPublicationState.STALE
                or current is None
                or str(current["head_sha"]) != str(row["head_sha"])
            )
            state = FindingPublicationState.STALE if stale else outcome.state
            retry_evidence = safe_publication_evidence(outcome.retry_evidence)
            if stale and outcome.state is not FindingPublicationState.STALE:
                retry_evidence = {"reason": "finding_head_changed_during_publish"}
            connection.execute(
                """
                    UPDATE review_findings
                    SET publication_state = ?, publication_channel = ?, remote_id = ?,
                        remote_url = ?, publication_retry_evidence_json = ?, stale = ?,
                        publication_claim_token = NULL,
                        publication_claim_expires_at = NULL, updated_at = ?
                    WHERE finding_id = ? AND publication_claim_token = ?
                    """,
                (
                    state.value,
                    (
                        row["publication_channel"]
                        if outcome.channel is None
                        else outcome.channel.value
                    ),
                    outcome.remote_id or row["remote_id"],
                    outcome.remote_url or row["remote_url"],
                    (
                        None
                        if retry_evidence is None
                        else json.dumps(retry_evidence, separators=(",", ":"))
                    ),
                    int(stale),
                    current_timestamp(),
                    finding_id,
                    claim_token,
                ),
            )
            return True
