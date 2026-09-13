from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

import pytest

from beehaiive.pbi_creation import (
    PbiCreationConflictError,
    PbiCreationProgress,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationScopeError,
    PbiCreationService,
    PbiCreationTarget,
    PbiCreationValidationError,
)
from beehaiive.provider import GitHubProjectProvider
from beehaiive.storage import OrchestratorStore


class FakePbiCreationProvider:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.create_calls = 0
        self.issue_posts = 0
        self.fail_after_issue = False
        self.unknown_create = False
        self.crash_after_create_marker = False
        self.reject_labels = False
        self.issue_exists = False
        self.entered: Event | None = None
        self.release: Event | None = None

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        self.prepare_calls += 1
        if request.repository != "owner/repo":
            raise AssertionError("unexpected repository")
        if self.reject_labels and request.labels:
            raise PbiCreationValidationError("Unknown label", code="unknown_label")
        return PbiCreationTarget(
            project_node_id="project-id",
            repository_node_id="repository-id",
            status_field_id="status-field",
            backlog_option_id="backlog-option",
            backlog_status="Backlog",
            label_ids=tuple(f"label-{name}" for name in request.labels),
        )

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint,
    ) -> PbiCreationResult:
        self.create_calls += 1
        current = progress
        if current.issue_id is None:
            self.issue_posts += 1
            current = replace(
                current,
                issue_create_started=True,
                current_step="create_issue",
            )
            checkpoint(current)
            if self.crash_after_create_marker:
                raise SystemExit("simulated process stop")
            if self.entered is not None and self.release is not None:
                self.entered.set()
                if not self.release.wait(timeout=5):
                    raise RuntimeError("test did not release blocked create")
            self.issue_exists = True
            if self.unknown_create:
                raise RuntimeError("private-provider-detail")
            current = replace(
                current,
                issue_create_started=False,
                issue_id="issue-node-id",
                issue_number=37,
                issue_url="https://example.test/issues/37",
                current_step="issue_created",
                completed_steps=(*current.completed_steps, "issue_created"),
            )
            checkpoint(current)
            if self.fail_after_issue:
                self.fail_after_issue = False
                raise RuntimeError("private-provider-detail")
        elif not self.issue_exists:
            raise AssertionError("retry lost the previously created issue")

        for step in ("labels_applied", "project_added", "status_backlog"):
            if step not in current.completed_steps:
                current = replace(
                    current,
                    current_step=step,
                    completed_steps=(*current.completed_steps, step),
                    project_item_id="project-item-id"
                    if step == "project_added"
                    else current.project_item_id,
                )
                checkpoint(current)
        return PbiCreationResult(
            issue_id="issue-node-id",
            issue_number=37,
            issue_url="https://example.test/issues/37",
            labels=request.labels,
            project_item_id="project-item-id",
            project_status="Backlog",
            completed_steps=current.completed_steps,
        )


class FakeCreationGraphQLClient:
    def __init__(
        self,
        labels: tuple[str, ...] = ("enhancement",),
        *,
        linked_repositories: tuple[str, ...] = ("owner/repo",),
        fail_after_step: str | None = None,
    ) -> None:
        self.labels = labels
        self.linked_repositories = linked_repositories
        self.fail_after_step = fail_after_step
        self.failure_emitted = False
        self.issue: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []
        self.other_items: list[dict[str, Any]] = [
            {
                "id": "draft-item-id",
                "content": {"__typename": "DraftIssue"},
                "fieldValues": {"nodes": []},
            },
            {"id": "deleted-content-item-id", "content": None},
        ]
        self.mutations: list[tuple[str, Mapping[str, object]]] = []
        self.rest_calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if "addLabelsToLabelable" in query:
            self.mutations.append((query, variables))
            return {"addLabelsToLabelable": {}}
        if "addProjectV2ItemById" in query:
            self.mutations.append((query, variables))
            self.items.append(
                {
                    "id": "project-item-id",
                    "content": {
                        "__typename": "Issue",
                        "id": "issue-node-id",
                        "number": 37,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    "fieldValues": {"nodes": [{}, {}, {}]},
                }
            )
            self._fail_once("project")
            return {"addProjectV2ItemById": {"item": {"id": "project-item-id"}}}
        if "updateProjectV2ItemFieldValue" in query:
            self.mutations.append((query, variables))
            self.items[0]["fieldValues"] = {
                "nodes": [
                    {
                        "name": "Backlog",
                        "optionId": "backlog-option",
                        "field": {"id": "status-field", "name": "Status"},
                    }
                ]
            }
            self._fail_once("backlog")
            return {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "project-item-id"}
                }
            }
        if "repositories(first: 100" in query:
            return self._project_data(
                repositories={
                    "nodes": [
                        {"nameWithOwner": name} for name in self.linked_repositories
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            )
        if "labels(first: 100, after:" in query:
            return {
                "repository": {
                    "id": "repository-id",
                    "nameWithOwner": "owner/repo",
                    "labels": {
                        "nodes": [
                            {"id": f"label-{name}", "name": name}
                            for name in self.labels
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        if "issue(number: $number)" in query:
            self._fail_once("labels")
            return {"repository": {"issue": self.issue}}
        if "items(first: 100, after:" in query:
            return self._project_data(
                items={
                    "nodes": [*self.other_items, *self.items],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            )
        raise AssertionError(f"Unexpected GraphQL query: {query}")

    def _fail_once(self, step: str) -> None:
        if self.fail_after_step == step and not self.failure_emitted:
            self.failure_emitted = True
            raise RuntimeError("private-provider-detail")

    def request_rest(
        self, method: str, path: str, payload: Mapping[str, object] | None
    ) -> tuple[int, Mapping[str, Any]]:
        self.rest_calls.append((method, path, payload))
        if method != "POST" or payload is None:
            raise AssertionError("Unexpected REST request")
        self.issue = {
            "id": "issue-node-id",
            "number": 37,
            "url": "https://example.test/issues/37",
            "title": payload["title"],
            "body": payload["body"],
            "state": "OPEN",
            "labels": {"nodes": [{"name": name} for name in payload["labels"]]},
        }
        return (
            201,
            {
                "node_id": "issue-node-id",
                "number": 37,
                "html_url": "https://example.test/issues/37",
            },
        )

    @staticmethod
    def _project_data(**values: object) -> dict[str, Any]:
        status_field = {
            "id": "status-field",
            "name": "Status",
            "options": [{"id": "backlog-option", "name": "Backlog"}],
        }
        project: dict[str, Any] = {
            "id": "project-node-id",
            "fields": {"nodes": [status_field]},
        }
        project.update(values)
        return {"user": {"projectV2": project}}


def make_request(
    body: str = "A body", labels: tuple[str, ...] = ()
) -> PbiCreationRequest:
    return PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body=body,
        labels=labels,
    )


def test_same_key_replays_result_and_conflicts_on_changed_content() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    service = PbiCreationService(store, provider)
    request = make_request()

    created = service.create(request, "request-1")
    replay = service.create(request, "request-1")

    assert created["status"] == "complete"
    assert replay == created
    assert provider.issue_posts == 1
    with pytest.raises(PbiCreationConflictError):
        service.create(make_request("Changed body"), "request-1")
    store.close()


def test_known_partial_issue_resumes_after_store_restart(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = OrchestratorStore(database)
    provider = FakePbiCreationProvider()
    provider.fail_after_issue = True
    request = make_request(labels=("enhancement",))

    incomplete = PbiCreationService(store, provider).create(request, "request-2")
    assert incomplete["status"] == "incomplete"
    assert incomplete["issue"] == {
        "id": "issue-node-id",
        "number": 37,
        "url": "https://example.test/issues/37",
    }
    assert "private-provider-detail" not in str(incomplete)
    store.close()

    restarted_store = OrchestratorStore(database)
    resumed = PbiCreationService(restarted_store, provider).create(request, "request-2")

    assert resumed["status"] == "complete"
    assert provider.issue_posts == 1
    restarted_store.close()


def test_ambiguous_create_never_retries_without_a_saved_issue_identity() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.unknown_create = True
    service = PbiCreationService(store, provider)
    request = make_request()

    first = service.create(request, "request-3")
    replay = service.create(request, "request-3")

    assert first["status"] == "outcome_unknown"
    assert first["issue"] is None
    assert first["operator_action_required"] is True
    assert replay == first
    assert provider.issue_posts == 1
    assert "private-provider-detail" not in str(first)
    store.close()


def test_expired_create_marker_after_restart_becomes_outcome_unknown(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = OrchestratorStore(database)
    provider = FakePbiCreationProvider()
    provider.crash_after_create_marker = True
    request = make_request()
    with pytest.raises(SystemExit, match="simulated process stop"):
        PbiCreationService(store, provider).create(request, "request-4")

    with store._lock:
        store._connection.execute(
            "UPDATE pbi_creations SET lease_expires_at = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    store.close()

    restarted_store = OrchestratorStore(database)
    result = PbiCreationService(restarted_store, provider).create(request, "request-4")

    assert result["status"] == "outcome_unknown"
    assert result["issue"] is None
    assert provider.issue_posts == 1
    assert provider.prepare_calls == 1
    restarted_store.close()


def test_concurrent_same_key_request_does_not_start_a_second_create() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.entered = Event()
    provider.release = Event()
    service = PbiCreationService(store, provider)
    request = make_request()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def create_first() -> None:
        try:
            results.append(service.create(request, "request-5"))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=create_first)
    worker.start()
    assert provider.entered.wait(timeout=2)
    concurrent = service.create(request, "request-5")
    provider.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert concurrent["status"] == "in_progress"
    assert results[0]["status"] == "complete"
    assert provider.issue_posts == 1
    store.close()


def test_unknown_label_is_rejected_before_any_issue_write() -> None:
    store = OrchestratorStore()
    provider = FakePbiCreationProvider()
    provider.reject_labels = True

    with pytest.raises(PbiCreationValidationError, match="Unknown label"):
        PbiCreationService(store, provider).create(
            make_request(labels=("missing",)), "request-6"
        )

    assert provider.issue_posts == 0
    store.close()


def test_github_provider_creates_issue_and_confirms_backlog_membership() -> None:
    request = PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body="A body",
        labels=("enhancement",),
    )
    client = FakeCreationGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    target = provider.prepare_pbi_creation(request)
    checkpoints: list[PbiCreationProgress] = []

    result = provider.create_pbi(
        request, target, PbiCreationProgress(), checkpoints.append
    )

    assert result.issue_number == 37
    assert result.labels == ("enhancement",)
    assert result.project_status == "Backlog"
    assert client.rest_calls == [
        (
            "POST",
            "/repos/owner/repo/issues",
            {
                "title": "Create a PBI",
                "body": "A body",
                "labels": ["enhancement"],
            },
        )
    ]
    assert len(client.mutations) == 2
    assert "addProjectV2ItemById" in client.mutations[0][0]
    assert "updateProjectV2ItemFieldValue" in client.mutations[1][0]
    assert checkpoints[-1].completed_steps == (
        "issue_created",
        "labels_applied",
        "project_added",
        "status_backlog",
    )


def test_github_provider_rejects_unknown_label_before_issue_creation() -> None:
    request = PbiCreationRequest(
        project_id="owner:7",
        repository="owner/repo",
        title="Create a PBI",
        body="A body",
        labels=("missing",),
    )
    client = FakeCreationGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    with pytest.raises(PbiCreationValidationError, match="labels do not exist"):
        provider.prepare_pbi_creation(request)

    assert client.rest_calls == []
    assert client.mutations == []


def test_github_provider_rejects_unlinked_repository_before_issue_creation() -> None:
    client = FakeCreationGraphQLClient(linked_repositories=())
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    with pytest.raises(PbiCreationScopeError, match="not linked"):
        provider.prepare_pbi_creation(make_request())

    assert client.rest_calls == []
    assert client.mutations == []


@pytest.mark.parametrize("failed_step", ("labels", "project", "backlog"))
def test_github_provider_retry_reconciles_after_remote_write(
    failed_step: str,
) -> None:
    store = OrchestratorStore()
    client = FakeCreationGraphQLClient(fail_after_step=failed_step)
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    service = PbiCreationService(store, provider)
    request = make_request(labels=("enhancement",))

    incomplete = service.create(request, f"retry-{failed_step}")
    completed = service.create(request, f"retry-{failed_step}")
    replay = service.create(request, f"retry-{failed_step}")

    assert incomplete["status"] == "incomplete"
    assert incomplete["issue"] == {
        "id": "issue-node-id",
        "number": 37,
        "url": "https://example.test/issues/37",
    }
    assert "private-provider-detail" not in str(incomplete)
    assert completed["status"] == "complete"
    assert replay == completed
    assert len(client.rest_calls) == 1
    assert sum("addProjectV2ItemById" in query for query, _ in client.mutations) == 1
    assert (
        sum("updateProjectV2ItemFieldValue" in query for query, _ in client.mutations)
        == 1
    )
    store.close()
