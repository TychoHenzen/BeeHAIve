from __future__ import annotations

import json
import sqlite3

from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult


def _task_claimability_state(
    contract_json: object,
    result_json: object,
    answer: object,
    answer_resumed: object,
) -> tuple[bool, bool]:
    if contract_json is None:
        return result_json is not None, False
    if not isinstance(contract_json, str) or not contract_json:
        return True, False
    if not isinstance(result_json, str) or not result_json:
        return True, False
    try:
        contract = TaskContract.from_dict(json.loads(contract_json))
        result = TaskResult.from_payload(
            json.loads(result_json), contract, allow_answer=True
        )
    except (ValueError, TypeError, RecursionError):
        return True, False
    answered_question = (
        result.outcome is TaskOutcome.QUESTION
        and result.answer is not None
        and result.answer == answer
        and bool(answer_resumed)
    )
    answered_blocker = (
        result.outcome is TaskOutcome.BLOCKED
        and isinstance(answer, str)
        and bool(answer.strip())
        and bool(answer_resumed)
    )
    paused = (result.outcome is TaskOutcome.BLOCKED and not answered_blocker) or (
        result.outcome is TaskOutcome.QUESTION and not answered_question
    )
    return paused, answered_question or answered_blocker


def _task_claimability_for_run(
    connection: sqlite3.Connection, run_id: str
) -> tuple[bool, bool]:
    row = connection.execute(
        """
        SELECT task_contract_json, task_result_json, task_answer,
               task_answer_resumed
        FROM runs WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return True, False
    return _task_claimability_state(
        row["task_contract_json"],
        row["task_result_json"],
        row["task_answer"],
        row["task_answer_resumed"],
    )


__all__ = ["_task_claimability_state", "_task_claimability_for_run"]
