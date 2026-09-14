from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi import Path as FastAPIPath
from fastapi.responses import JSONResponse

from beehaiive.api.models import PbiCreationBody as PbiCreationBody
from beehaiive.api.models import PbiRelationsBody as PbiRelationsBody
from beehaiive.pbi_creation import PbiCreationError, PbiCreationRequest
from beehaiive.pbi_relations import (
    PbiCreatedIssueReference,
    PbiRelationDependency,
    PbiRelationError,
    PbiRelationRequest,
)
from beehaiive.storage import StoreError


def register_routes(app: FastAPI, context: dict[str, Any]) -> None:
    pbi_creation_service = context["pbi_creation_service"]
    pbi_relations_service = context["pbi_relations_service"]
    require_mutation_access = context["require_mutation_access"]

    @app.post("/projects/{project_id}/pbis")
    def create_project_pbi(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        request: PbiCreationBody,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=200
        ),
        _auth: None = Depends(require_mutation_access),
    ) -> JSONResponse:
        creation_request = PbiCreationRequest(
            project_id=project_id,
            repository=request.repository,
            title=request.title,
            body=request.body,
            labels=tuple(request.labels),
        )
        try:
            result = pbi_creation_service.create(creation_request, idempotency_key)
        except PbiCreationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        status_code = 201 if result.get("status") == "complete" else 202
        return JSONResponse(status_code=status_code, content=result)

    @app.post(
        "/projects/{project_id}/repositories/{repository:path}/pbis/{pbi_number}/relations"
    )
    def apply_project_pbi_relations(  # pyright: ignore[reportUnusedFunction]
        project_id: str,
        repository: Annotated[str, FastAPIPath(min_length=3, max_length=300)],
        pbi_number: Annotated[int, FastAPIPath(gt=0, le=2_147_483_647)],
        request: PbiRelationsBody,
        _auth: None = Depends(require_mutation_access),
    ) -> JSONResponse:
        relation_request = PbiRelationRequest(
            project_id=project_id,
            repository=repository,
            parent_issue_number=pbi_number,
            children=tuple(
                PbiCreatedIssueReference(
                    repository=child.repository,
                    node_id=child.issue.id,
                    number=child.issue.number,
                    url=child.issue.url,
                    project_item_id=child.project.item_id,
                )
                for child in request.children
            ),
            dependencies=tuple(
                PbiRelationDependency(
                    edge.blocked_issue_number,
                    edge.blocked_by_issue_number,
                )
                for edge in request.dependencies
            ),
        )
        try:
            result = pbi_relations_service.apply(relation_request)
        except PbiRelationError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "detail": "Relation request was rejected",
                    }
                },
            )
        status_code = 200 if result.status == "complete" else 202
        return JSONResponse(status_code=status_code, content=result.as_dict())
