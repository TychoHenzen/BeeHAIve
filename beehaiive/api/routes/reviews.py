from typing import Any

from fastapi import Depends, FastAPI

from beehaiive.api.helpers.http import _handle_review_error as _handle_review_error
from beehaiive.api.models import ReviewApprovalRequest as ReviewApprovalRequest
from beehaiive.api.models import ReviewFindingRequest as ReviewFindingRequest
from beehaiive.api.models import ReviewHandoffRequest as ReviewHandoffRequest
from beehaiive.api.models import ReviewReaderRequest as ReviewReaderRequest
from beehaiive.api.models import ReviewReadyRequest as ReviewReadyRequest
from beehaiive.api.models import ReviewRepairRequest as ReviewRepairRequest
from beehaiive.api.models import ReviewResolutionRequest as ReviewResolutionRequest
from beehaiive.api.models import ReviewStartRequest as ReviewStartRequest
from beehaiive.review import ReviewAction, ReviewError
from beehaiive.storage import StoreError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    orchestrator = context["orchestrator"]
    require_review_access = context["require_review_access"]
    require_review_repair_service = context["require_review_repair_service"]
    review_service = context["review_service"]

    @app.post("/reviews/ready")
    def run_ready_review(  # pyright: ignore[reportUnusedFunction]
        request: ReviewReadyRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(request.pull_request_id, actor, ReviewAction.START)
            return review_service.run_ready_review(request.pull_request_id).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles")
    def start_review_cycle(  # pyright: ignore[reportUnusedFunction]
        request: ReviewStartRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(request.pull_request_id, actor, ReviewAction.START)
            return review_service.start_cycle(
                request.pull_request_id, request.head_sha
            ).as_dict()

        return _handle_review_error(operation)

    @app.get("/reviews/pull-requests/{pull_request_id:path}")
    def review_state(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, ReviewAction.READ)
            return review_service.snapshot(pull_request_id).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/readers")
    def record_review_reader(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewReaderRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, ReviewAction.READER)
            return review_service.record_reader(
                cycle_id,
                request.concern,
                request.status,
                request.findings,
                request.reader,
                evidence_refs=request.evidence_refs,
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/findings")
    def add_review_finding(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewFindingRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, ReviewAction.WRITER)
            return review_service.add_finding(
                cycle_id,
                request.concern,
                request.summary,
                evidence_refs=request.evidence_refs,
                file_path=request.file_path,
                start_line=request.start_line,
                end_line=request.end_line,
                duplicate_target=request.duplicate_target,
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/findings/{finding_id}/resolve")
    def resolve_review_finding(  # pyright: ignore[reportUnusedFunction]
        finding_id: str,
        request: ReviewResolutionRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_finding(finding_id)
            review_service.authorize(pull_request_id, actor, ReviewAction.WRITER)
            return review_service.resolve_finding(
                finding_id, request.resolution, actor=actor
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/findings/{finding_id}/publish")
    def publish_review_finding(  # pyright: ignore[reportUnusedFunction]
        finding_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_finding(finding_id)
            review_service.authorize(pull_request_id, actor, ReviewAction.PUBLISH)
            return review_service.publish_finding(finding_id).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/cycles/{cycle_id}/repair")
    def dispatch_review_repair(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewRepairRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        return _handle_review_error(
            lambda: (
                require_review_repair_service()
                .dispatch(cycle_id, tuple(request.finding_ids), actor)
                .as_dict()
            )
        )

    @app.get("/reviews/repairs/{attempt_id}")
    def review_repair_state(  # pyright: ignore[reportUnusedFunction]
        attempt_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            attempt = review_service.repair_attempt(attempt_id)
            review_service.authorize(attempt.pull_request_id, actor, ReviewAction.READ)
            return require_review_repair_service().get(attempt_id).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/repairs/{attempt_id}/cancel")
    def cancel_review_repair(  # pyright: ignore[reportUnusedFunction]
        attempt_id: str,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        return _handle_review_error(
            lambda: require_review_repair_service().cancel(attempt_id, actor).as_dict()
        )

    @app.post("/reviews/cycles/{cycle_id}/approve")
    def approve_review_cycle(  # pyright: ignore[reportUnusedFunction]
        cycle_id: str,
        request: ReviewApprovalRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            pull_request_id = review_service.pull_request_id_for_cycle(cycle_id)
            review_service.authorize(pull_request_id, actor, ReviewAction.APPROVE)
            return review_service.approve_for_merge(
                cycle_id, request.reason, actor
            ).as_dict()

        return _handle_review_error(operation)

    @app.post("/reviews/pull-requests/{pull_request_id}/handoff")
    def review_handoff(  # pyright: ignore[reportUnusedFunction]
        pull_request_id: str,
        request: ReviewHandoffRequest,
        actor: str = Depends(require_review_access),
    ) -> dict[str, object]:
        def operation() -> dict[str, object]:
            review_service.authorize(pull_request_id, actor, ReviewAction.HANDOFF)
            try:
                authorization = review_service.merge_handoff(
                    pull_request_id, request.head_sha
                )
            except ReviewError as authorization_error:
                try:
                    completion = orchestrator.complete_approved_handoff(
                        pull_request_id, request.head_sha, None
                    )
                except StoreError:
                    raise authorization_error from None
                if completion.get("reason") == "current_review_authorization_required":
                    raise authorization_error
                return {
                    "pull_request_id": pull_request_id,
                    "head_sha": request.head_sha,
                    "status": "completion_resume",
                    "completion": dict(completion),
                }
            try:
                completion = orchestrator.complete_approved_handoff(
                    pull_request_id,
                    request.head_sha,
                    authorization.as_dict(),
                )
            except StoreError as exc:
                raise ReviewError(str(exc)) from exc
            return {**authorization.as_dict(), "completion": dict(completion)}

        return _handle_review_error(operation)
