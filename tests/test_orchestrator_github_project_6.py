from dataclasses import replace

import pytest

import beehaiive.provider as provider_module
from beehaiive.models import (
    HandoffRequest,
)
from beehaiive.provider import (
    GitHubProjectProvider,
    ProviderError,
    _handoff_marker,
)
from tests.support.orchestrator.handoff_graph_ql_client import (
    HandoffGraphQLClient as HandoffGraphQLClient,
)


def test_github_provider_creates_and_updates_verified_draft() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    client.branch_sha = "a" * 40
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
        head_sha="a" * 40,
        verification_evidence='{"outcome":"pass"}',
    )

    created = provider.create_handoff(request)
    updated = provider.create_handoff(
        HandoffRequest(
            project_id="owner:7",
            repository="owner/api",
            pbi_number=1,
            title="API one updated",
            branch="codex/api-1",
            base_branch=None,
            body="Implementation summary",
            run_id="run-1",
            head_sha="a" * 40,
            verification_evidence='{"outcome":"pass","tests":12}',
        )
    )

    assert created == updated
    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 1
    assert client.created_pull_request_inputs[0]["draft"] is True
    assert (
        client.updated_pull_request_inputs[0]["pullRequestId"] == "pull-request-node-8"
    )
    pull_request = client.pull_requests[0]
    assert pull_request["title"] == "API one updated"
    body = str(pull_request["body"])
    assert "https://github.com/owner/api/issues/1" in body
    assert "run-1" in body
    assert "a" * 40 in body
    assert '{"outcome":"pass","tests":12}' in body
    assert _handoff_marker(request, "main") in body


def test_github_provider_does_not_reuse_pull_request_for_changed_body_intent() -> None:
    client = HandoffGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
    )
    provider.create_handoff(request)

    with pytest.raises(ProviderError, match="different handoff identity"):
        provider.create_handoff(replace(request, body="Changed implementation summary"))

    assert client.pull_request_creations == 1
    assert client.pull_request_updates == 0


def test_github_provider_upgrades_exact_legacy_handoff_marker() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
    )
    legacy_marker = provider_module._legacy_handoff_marker(request)
    legacy_body = provider_module._handoff_body(request.body, legacy_marker, request)
    client.pull_requests.append(
        {
            "id": "pull-request-node-8",
            "number": 8,
            "url": "https://example.test/owner/api/pull/8",
            "title": request.title,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": request.branch,
            "headRefOid": client.branch_sha,
            "baseRefName": "main",
            "body": legacy_body,
        }
    )

    result = provider.create_handoff(request)

    assert result.pull_request_number == 8
    assert client.pull_request_creations == 0
    assert client.pull_request_updates == 1
    assert _handoff_marker(request, "main") in client.pull_requests[0]["body"]


def test_github_provider_does_not_upgrade_legacy_pr_for_changed_body_intent() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    original = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Original implementation summary",
        run_id="run-1",
    )
    legacy_marker = provider_module._legacy_handoff_marker(original)
    client.pull_requests.append(
        {
            "id": "pull-request-node-8",
            "number": 8,
            "url": "https://example.test/owner/api/pull/8",
            "title": original.title,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": original.branch,
            "headRefOid": client.branch_sha,
            "baseRefName": "main",
            "body": provider_module._handoff_body(
                original.body, legacy_marker, original
            ),
        }
    )

    with pytest.raises(ProviderError, match="different handoff identity"):
        provider.create_handoff(
            replace(original, body="Changed implementation summary")
        )

    assert client.pull_request_updates == 0
    assert client.pull_request_creations == 0


def test_github_provider_adds_verified_metadata_when_body_has_marker() -> None:
    client = HandoffGraphQLClient()
    client.ref_exists = True
    client.branch_sha = "a" * 40
    provider = GitHubProjectProvider("owner", 7, "token", client=client)
    request = HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=None,
        body="Implementation summary",
        run_id="run-1",
        head_sha="a" * 40,
        verification_evidence='{"outcome":"pass"}',
    )
    request = replace(request, body=_handoff_marker(request, "main"))

    provider.create_handoff(request)

    body = str(client.created_pull_request_inputs[0]["body"])
    assert "https://github.com/owner/api/issues/1" in body
    assert "Run: `run-1`" in body
    assert "Pushed head: `" + "a" * 40 + "`" in body
    assert '{"outcome":"pass"}' in body
    assert body.count(_handoff_marker(request, "main")) == 1
