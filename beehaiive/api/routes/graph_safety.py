from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from beehaiive.api.models import (
    GraphRollbackRequest,
    GraphSafetyRequest,
)
from beehaiive.contracts import ContractError
from beehaiive.graph import GraphDefinition, GraphDefinitionError
from beehaiive.graph_safety import GraphSafetyError, GraphSafetyService
from beehaiive.storage import StoreError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    service: GraphSafetyService = context["graph_safety_service"]
    require_workflow_access = context["require_workflow_access"]
    require_workflow_operator = context["require_workflow_operator"]

    def evaluate_request(request: GraphSafetyRequest):
        try:
            candidate = GraphDefinition.from_dict(request.candidate)
            baseline = (
                GraphDefinition.from_dict(request.baseline)
                if request.baseline is not None
                else None
            )
            return service.evaluate(
                candidate,
                request.fixtures,
                baseline=baseline,
                baseline_fixtures=request.baseline_fixtures,
            )
        except (
            ContractError,
            GraphDefinitionError,
            GraphSafetyError,
            StoreError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflow/graphs/safety/evaluate")
    def evaluate_graph_safety(  # pyright: ignore[reportUnusedFunction]
        request: GraphSafetyRequest,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        return evaluate_request(request).as_dict()

    @app.post("/workflow/graphs/safety/review")
    def review_graph_safety(  # pyright: ignore[reportUnusedFunction]
        request: GraphSafetyRequest,
        actor: str = Depends(require_workflow_operator),
    ) -> dict[str, object]:
        evaluation = evaluate_request(request)
        try:
            review = service.review(evaluation, actor)
        except (GraphSafetyError, StoreError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"evaluation": evaluation.as_dict(), "review": review.as_dict()}

    @app.post("/workflow/graphs/safety/activate")
    def activate_graph_safety(  # pyright: ignore[reportUnusedFunction]
        request: GraphSafetyRequest,
        actor: str = Depends(require_workflow_operator),
    ) -> dict[str, object]:
        evaluation = evaluate_request(request)
        try:
            activation = service.activate(evaluation, actor)
        except (GraphSafetyError, StoreError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"evaluation": evaluation.as_dict(), "activation": activation.as_dict()}

    @app.post("/workflow/graphs/safety/rollback")
    def rollback_graph_safety(  # pyright: ignore[reportUnusedFunction]
        request: GraphRollbackRequest,
        actor: str = Depends(require_workflow_operator),
    ) -> dict[str, object]:
        try:
            activation = service.rollback(request.workflow_id, request.revision, actor)
        except (GraphSafetyError, StoreError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return activation.as_dict()

    @app.get("/workflow/graphs/{workflow_id:path}/active")
    def active_graph_version(  # pyright: ignore[reportUnusedFunction]
        workflow_id: str,
        _auth: None = Depends(require_workflow_access),
    ) -> dict[str, object]:
        try:
            activation = service.active(workflow_id)
        except (GraphSafetyError, StoreError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if activation is None:
            raise HTTPException(
                status_code=404, detail="Active graph version not found"
            )
        return activation.as_dict()


__all__ = ["register_routes"]
