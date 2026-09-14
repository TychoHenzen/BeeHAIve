from beehaiive.models import RunState


def _run_dict(
    run: RunState, routing: dict[str, object] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "repository": run.repository,
        "pbi_number": run.pbi_number,
        "title": run.title,
        "stage": run.stage.value,
        "status": run.status.value,
        "attempt": run.attempt,
        "branch": run.branch,
        "pull_request_url": run.pull_request_url,
        "last_error": run.last_error,
        "result": run.last_result,
        "owner_id": run.owner_id,
        "lease_token": run.lease_token,
        "lease_expires_at": run.lease_expires_at,
        "task_contract": run.task_contract,
        "task_result": run.task_result,
        "task_answer": run.task_answer,
    }
    if routing is not None:
        result["routing"] = routing
    return result


def _public_run_dict(run: RunState) -> dict[str, object]:
    result = _run_dict(run)
    result.pop("lease_token", None)
    return result


__all__ = ["_run_dict", "_public_run_dict"]
