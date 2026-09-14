import subprocess
from pathlib import Path

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import (
    GitHubProjectProvider,
    GraphQLClient,
)
from beehaiive.storage import OrchestratorStore
from tests.support.orchestrator.disposable_repository_provider import (
    DisposableRepositoryProvider as DisposableRepositoryProvider,
)
from tests.support.orchestrator.fake_graph_ql_client import (
    FakeGraphQLClient as FakeGraphQLClient,
)
from tests.support.orchestrator.helpers import pull_request_snapshot as snapshot
from tests.support.orchestrator.nested_metadata_graph_ql_client import (
    NestedMetadataGraphQLClient as NestedMetadataGraphQLClient,
)
from tests.support.orchestrator.paginated_graph_ql_client import (
    PaginatedGraphQLClient as PaginatedGraphQLClient,
)


def test_handoff_records_branch_and_pull_request_in_disposable_repository(
    tmp_path: Path,
) -> None:
    repository_path = tmp_path / "disposable-repository"
    repository_path.mkdir()
    subprocess.run(
        ["git", "init", str(repository_path)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "config",
            "user.email",
            "test@example.test",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    (repository_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    provider = DisposableRepositoryProvider(repository_path, snapshot())
    service = Orchestrator(OrchestratorStore(), provider)
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    completed = service.handoff(
        run.run_id,
        "codex/api-1",
        "master",
        "Closes #1",
        lease_token,
    )
    branches = subprocess.run(
        ["git", "-C", str(repository_path), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert completed.branch == "codex/api-1"
    assert "codex/api-1" in branches
    assert provider.pull_request_records == [
        {
            "branch": "codex/api-1",
            "url": "https://example.test/owner/api/pull/1",
        }
    ]


def test_github_provider_discovers_linked_repositories_without_items() -> None:
    client: GraphQLClient = FakeGraphQLClient(
        {
            "user": {
                "projectV2": {
                    "title": "Planning",
                    "repositories": {
                        "nodes": [
                            {"nameWithOwner": "owner/api"},
                            {"nameWithOwner": "owner/web"},
                        ]
                    },
                    "items": {
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 1,
                                    "title": "API one",
                                    "repository": {"nameWithOwner": "owner/api"},
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {},
                                        {},
                                        {
                                            "name": "Backlog",
                                            "field": {"name": "Status"},
                                        },
                                    ]
                                },
                            }
                        ]
                    },
                }
            }
        }
    )
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")

    assert [repo.name for repo in discovered.repositories] == ["owner/api", "owner/web"]
    assert discovered.repositories[0].pbis[0].stage is Stage.BACKLOG


def test_github_provider_paginates_repositories_and_items() -> None:
    client = PaginatedGraphQLClient()
    provider = GitHubProjectProvider("owner", 7, "token", client=client)

    discovered = provider.discover_project("owner:7")

    assert len(discovered.repositories) == 101
    assert [pbi.number for pbi in discovered.repositories[0].pbis] == [1, 2]


def test_github_provider_paginates_nested_dashboard_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner", 7, "token", client=NestedMetadataGraphQLClient()
    )

    discovered = provider.discover_project("owner:7")
    metadata = discovered.repositories[0].pbis[0].metadata

    assert len(metadata["subtasks"]) == 101  # type: ignore[arg-type]
    assert metadata["subtasks"][0]["labels"] == ["stage/implement"]  # type: ignore[index]
    assert len(metadata["activity"]) == 101  # type: ignore[arg-type]
    assert len(metadata["pull_requests"]) == 101  # type: ignore[arg-type]
    assert metadata["reviewers"]["#1:reviewer-101"]["status"] == "pass"  # type: ignore[index]
    assert metadata["escalation"] == {
        "current": 2,
        "consecutive": 2,
        "current_tier": "terra",
    }
    readiness = metadata["dependency_readiness"]  # type: ignore[index]
    assert readiness["status"] == "unknown"  # type: ignore[index]
    assert readiness["counts"]["unknown"] == 101  # type: ignore[index]
