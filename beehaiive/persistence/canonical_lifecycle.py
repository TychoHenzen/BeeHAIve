from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from beehaiive.lifecycle_contract import (
    MAX_TRANSITION_EVIDENCE_RECORDS,
    FactOwner,
    LifecycleState,
    TransitionEvidence,
    TransitionReason,
    can_transition,
)
from beehaiive.lifecycle_projection import derive_lifecycle_projection
from beehaiive.models import ProjectSnapshot

from .errors import StoreError
from .helpers.lease_helpers import _now as _now


class CanonicalLifecycleMixin:
    def canonical_source_versions(
        self: Any, project_id: str
    ) -> dict[tuple[str, int], str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT repository_name, number, canonical_source_version
                FROM pbis
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()
            return {
                (str(row["repository_name"]), int(row["number"])): str(
                    row["canonical_source_version"] or ""
                )
                for row in rows
            }

    def project_canonical_lifecycle(
        self: Any,
        snapshot: ProjectSnapshot,
        optional_sources: Mapping[tuple[str, int], Mapping[str, Mapping[str, object]]]
        | None = None,
        expected_source_versions: Mapping[tuple[str, int], str] | None = None,
    ) -> tuple[dict[str, object], ...]:
        if not snapshot.project_id.strip():
            raise StoreError("A project id is required")
        projections: list[dict[str, object]] = []
        with self._transaction() as connection:
            for repository in snapshot.repositories:
                for pbi in repository.pbis:
                    row = connection.execute(
                        """
                        SELECT canonical_state, canonical_source_version, run_id,
                               EXISTS(
                                   SELECT 1
                                   FROM lifecycle_transition_evidence AS e
                                   WHERE e.project_id = pbis.project_id
                                     AND e.repository_name = pbis.repository_name
                                     AND e.pbi_number = pbis.number
                               ) AS has_evidence
                        FROM pbis
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (snapshot.project_id, repository.name, pbi.number),
                    ).fetchone()
                    if row is None:
                        continue
                    run_id = row["run_id"]
                    run = (
                        self._run_for_id(connection, str(run_id))
                        if run_id is not None
                        else None
                    )
                    source_key = (repository.name, pbi.number)
                    projection = derive_lifecycle_projection(
                        pbi,
                        run,
                        (optional_sources or {}).get(source_key),
                    )
                    state = projection.state
                    reason_code = projection.reason_code
                    previous = _state_or_none(row["canonical_state"])
                    current_version = row["canonical_source_version"]
                    expected_version = (expected_source_versions or {}).get(source_key)
                    if (
                        (
                            row["has_evidence"]
                            and previous is LifecycleState.UNKNOWN
                            and current_version == projection.source_version
                        )
                        or (
                            expected_version is not None
                            and expected_version != current_version
                        )
                        or (
                            expected_source_versions is not None
                            and row["has_evidence"]
                            and expected_version is None
                        )
                    ):
                        state = LifecycleState.UNKNOWN
                        reason_code = TransitionReason.CONFLICT
                    before = previous if row["has_evidence"] and previous else state
                    if previous is LifecycleState.COMPLETED and state is not previous:
                        state = LifecycleState.COMPLETED
                        reason_code = TransitionReason.CONFLICT
                        before = previous
                    elif before is not state and not can_transition(before, state):
                        state = (
                            LifecycleState.UNKNOWN
                            if can_transition(before, LifecycleState.UNKNOWN)
                            else before
                        )
                        reason_code = TransitionReason.CONFLICT
                    event_type = (
                        "canonical_conflict"
                        if reason_code is TransitionReason.CONFLICT
                        else "canonical_projection"
                    )
                    observed_at = _now()
                    source_id = f"{snapshot.project_id}:{repository.name}#{pbi.number}"
                    evidence = TransitionEvidence(
                        item_id=source_id,
                        event_type=event_type,
                        source_owner=FactOwner.DERIVED,
                        source_id=source_id,
                        source_version=projection.source_version,
                        observed_at=observed_at,
                        state_before=before,
                        state_after=state,
                        reason_code=reason_code,
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO lifecycle_transition_evidence(
                            project_id, repository_name, pbi_number, replay_id,
                            event_type, source_owner, source_id, source_version,
                            observed_at, state_before, state_after, reason_code,
                            schema_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot.project_id,
                            repository.name,
                            pbi.number,
                            evidence.replay_id,
                            evidence.event_type,
                            evidence.source_owner.value,
                            evidence.source_id,
                            projection.source_version,
                            evidence.observed_at,
                            evidence.state_before.value,
                            evidence.state_after.value,
                            reason_code.value,
                            evidence.schema_version,
                            observed_at,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM lifecycle_transition_evidence
                        WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                          AND evidence_id NOT IN (
                              SELECT evidence_id
                              FROM lifecycle_transition_evidence
                              WHERE project_id = ?
                                AND repository_name = ?
                                AND pbi_number = ?
                              ORDER BY evidence_id DESC
                              LIMIT ?
                          )
                        """,
                        (
                            snapshot.project_id,
                            repository.name,
                            pbi.number,
                            snapshot.project_id,
                            repository.name,
                            pbi.number,
                            MAX_TRANSITION_EVIDENCE_RECORDS,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE pbis
                        SET canonical_state = ?, canonical_facts_json = ?,
                            canonical_source_version = ?
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (
                            state.value,
                            json.dumps(dict(projection.facts), sort_keys=True),
                            projection.source_version,
                            snapshot.project_id,
                            repository.name,
                            pbi.number,
                        ),
                    )
                    projections.append(
                        {
                            "repository": repository.name,
                            "pbi_number": pbi.number,
                            "state": state.value,
                            "source_version": projection.source_version,
                            "reason_code": reason_code.value,
                            "replay_id": evidence.replay_id,
                        }
                    )
        return tuple(projections)

    def canonical_lifecycle_for(
        self: Any, project_id: str, repository: str, pbi_number: int
    ) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT canonical_state, canonical_facts_json,
                       canonical_source_version
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (project_id, repository, pbi_number),
            ).fetchone()
            if row is None:
                return None
            try:
                facts = json.loads(str(row["canonical_facts_json"]))
            except json.JSONDecodeError:
                facts = {}
            return {
                "state": row["canonical_state"],
                "facts": facts if isinstance(facts, dict) else {},
                "source_version": row["canonical_source_version"],
            }

    def lifecycle_evidence_for(
        self: Any, project_id: str, repository: str, pbi_number: int
    ) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT replay_id, event_type, source_owner, source_id,
                       source_version, observed_at, state_before, state_after,
                       reason_code, schema_version, created_at
                FROM lifecycle_transition_evidence
                WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
                ORDER BY evidence_id
                """,
                (project_id, repository, pbi_number),
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def lifecycle_evidence_for_project(
        self: Any, project_id: str
    ) -> dict[tuple[str, int], list[dict[str, object]]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT repository_name, pbi_number, replay_id, event_type,
                       source_owner, source_id, source_version, observed_at,
                       state_before, state_after, reason_code, schema_version,
                       created_at
                FROM (
                    SELECT repository_name, pbi_number, replay_id, event_type,
                           source_owner, source_id, source_version, observed_at,
                           state_before, state_after, reason_code, schema_version,
                           created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY repository_name, pbi_number
                               ORDER BY evidence_id
                           ) AS evidence_rank
                    FROM lifecycle_transition_evidence
                    WHERE project_id = ?
                )
                WHERE evidence_rank <= ?
                ORDER BY repository_name, pbi_number, evidence_rank
                """,
                (project_id, MAX_TRANSITION_EVIDENCE_RECORDS),
            ).fetchall()
            evidence_by_pbi: dict[tuple[str, int], list[dict[str, object]]] = {}
            for row in rows:
                key = (str(row["repository_name"]), int(row["pbi_number"]))
                evidence_by_pbi.setdefault(key, []).append(
                    {
                        key_name: row[key_name]
                        for key_name in (
                            "replay_id",
                            "event_type",
                            "source_owner",
                            "source_id",
                            "source_version",
                            "observed_at",
                            "state_before",
                            "state_after",
                            "reason_code",
                            "schema_version",
                            "created_at",
                        )
                    }
                )
            return evidence_by_pbi


def _state_or_none(value: object) -> LifecycleState | None:
    if not isinstance(value, str):
        return None
    try:
        return LifecycleState(value)
    except ValueError:
        return None


__all__ = ["CanonicalLifecycleMixin"]
