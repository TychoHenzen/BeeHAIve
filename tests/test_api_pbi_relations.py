from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.pbi_relations import (
    PbiRelationIssue,
    PbiRelationRequest,
    PbiRelationSnapshot,
    PbiRelationValidationError,
)
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def test_pbi_relations_route_is_authenticated_scoped_and_redacted() -> None:
    class RelationApiProvider(ApiProvider):
        def __init__(self) -> None:
            super().__init__()
            self.relation_prepare_calls = 0
            self.sub_issues: set[int] = set()
            self.sub_issue_parents: dict[int, int] = {}
            self.sub_issue_writes: list[tuple[int, int]] = []

        @staticmethod
        def _issue(number: int, url: str | None = None) -> PbiRelationIssue:
            return PbiRelationIssue(
                100 + number,
                f"node-{number}",
                number,
                url or f"https://example.test/issues/{number}",
                "private-token-must-not-appear",
                "OPEN",
            )

        def prepare_pbi_relations(
            self, request: PbiRelationRequest
        ) -> PbiRelationSnapshot:
            self.relation_prepare_calls += 1
            return PbiRelationSnapshot(
                self._issue(request.parent_issue_number),
                tuple(
                    self._issue(child.number, child.url) for child in request.children
                ),
                tuple(self._issue(number) for number in sorted(self.sub_issues)),
                {
                    child.number: self.sub_issue_parents.get(child.number)
                    for child in request.children
                },
            )

        def list_pbi_sub_issues(
            self, repository: str, parent_issue_number: int
        ) -> tuple[PbiRelationIssue, ...]:
            assert repository == "owner/api"
            assert parent_issue_number == 1
            return tuple(self._issue(number) for number in sorted(self.sub_issues))

        def get_pbi_parent_issue_number(
            self, repository: str, child_issue_number: int
        ) -> int | None:
            assert repository == "owner/api"
            return self.sub_issue_parents.get(child_issue_number)

        def list_pbi_blocked_by(
            self, repository: str, issue_number: int
        ) -> tuple[PbiRelationIssue, ...]:
            assert repository == "owner/api"
            assert issue_number > 0
            return ()

        def list_pbi_blocking(
            self, repository: str, issue_number: int
        ) -> tuple[PbiRelationIssue, ...]:
            assert repository == "owner/api"
            assert issue_number > 0
            return ()

        def add_pbi_sub_issue(
            self, repository: str, parent_issue_number: int, child_issue_id: int
        ) -> None:
            assert repository == "owner/api"
            self.sub_issue_writes.append((parent_issue_number, child_issue_id))
            child_number = child_issue_id - 100
            self.sub_issues.add(child_number)
            self.sub_issue_parents[child_number] = parent_issue_number

        def add_pbi_dependency(
            self, repository: str, blocked_issue_number: int, blocker_issue_id: int
        ) -> None:
            raise AssertionError(
                "Unexpected dependency write: "
                f"{repository} {blocked_issue_number} {blocker_issue_id}"
            )

    store = OrchestratorStore()
    provider = RelationApiProvider()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, provider),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    path = "/projects/owner:7/repositories/owner/api/pbis/1/relations"
    headers = {"X-API-Key": "test-key"}
    child = {
        "status": "complete",
        "repository": "owner/api",
        "issue": {
            "id": "node-2",
            "number": 2,
            "url": "https://example.test/issues/2",
        },
        "project": {"item_id": "child-item", "status": "Backlog"},
    }

    unauthenticated = project_client.post(path, json={"children": [child]})
    synced = project_client.post("/projects/owner:7/sync", headers=headers)
    unauthorized_repository = project_client.post(
        "/projects/owner:7/repositories/owner/other/pbis/1/relations",
        headers=headers,
        json={"children": [child]},
    )
    cross_repository = project_client.post(
        path,
        headers=headers,
        json={"children": [{**child, "repository": "owner/other"}]},
    )
    malformed_reference = project_client.post(
        path,
        headers=headers,
        json={
            "children": [
                {
                    **child,
                    "issue": {
                        "id": "node-2",
                        "number": "2",
                        "url": "https://example.test/issues/2",
                    },
                }
            ]
        },
    )
    applied = project_client.post(path, headers=headers, json={"children": [child]})

    assert unauthenticated.status_code == 401
    assert synced.status_code == 200
    assert unauthorized_repository.status_code == 403
    assert cross_repository.status_code == 422
    assert malformed_reference.status_code == 422
    assert provider.relation_prepare_calls == 1
    assert applied.status_code == 200
    assert applied.json()["relations"]["sub_issues"]["confirmed"] == [
        {"parent_issue_number": 1, "child_issue_number": 2}
    ]
    assert "private-token-must-not-appear" not in applied.text
    assert provider.sub_issue_writes == [(1, 102)]
    store.close()


def test_pbi_relations_route_redacts_exception_details() -> None:
    class ErrorProvider(ApiProvider):
        def prepare_pbi_relations(self, request: PbiRelationRequest):
            del request
            raise PbiRelationValidationError(
                "private-token-must-not-appear", code="invalid_parent"
            )

    store = OrchestratorStore()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ErrorProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    path = "/projects/owner:7/repositories/owner/api/pbis/1/relations"
    headers = {"X-API-Key": "test-key"}
    child = {
        "status": "complete",
        "repository": "owner/api",
        "issue": {
            "id": "node-2",
            "number": 2,
            "url": "https://example.test/issues/2",
        },
        "project": {"item_id": "child-item", "status": "Backlog"},
    }
    project_client.post("/projects/owner:7/sync", headers=headers)

    response = project_client.post(path, headers=headers, json={"children": [child]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parent"
    assert response.json()["error"]["detail"] == "Relation request was rejected"
    assert "private-token-must-not-appear" not in response.text
    store.close()
