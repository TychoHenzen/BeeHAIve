from __future__ import annotations

from dataclasses import replace

import pytest

from beehaiive.pbi_relations import (
    PbiCreatedIssueReference,
    PbiRelationDependency,
    PbiRelationRequest,
    PbiRelationService,
    PbiRelationValidationError,
)
from tests.support.fake_relation_provider import FakeRelationProvider


def _request(
    dependencies: tuple[PbiRelationDependency, ...] = (),
) -> PbiRelationRequest:
    return PbiRelationRequest(
        project_id="owner:2",
        repository="owner/repo",
        parent_issue_number=1,
        children=tuple(
            PbiCreatedIssueReference(
                repository="owner/repo",
                node_id=f"node-{number}",
                number=number,
                url=f"https://github.com/owner/repo/issues/{number}",
                project_item_id=f"item-{number}",
            )
            for number in (2, 3)
        ),
        dependencies=dependencies,
    )


def test_relations_are_idempotent_and_read_back_from_provider() -> None:
    provider = FakeRelationProvider()
    request = _request((PbiRelationDependency(3, 2),))
    service = PbiRelationService(provider)

    first = service.apply(request)
    second = service.apply(request)

    assert first.status == "complete"
    assert first.confirmed_children == (2, 3)
    assert first.confirmed_dependencies == (PbiRelationDependency(3, 2),)
    assert second.status == "complete"
    assert len(provider.added_sub_issues) == 2
    assert provider.added_dependencies == [(3, 102)]


def test_dependency_cycle_is_rejected_before_any_relation_write() -> None:
    provider = FakeRelationProvider()
    provider.blocked_by[3].add(2)

    with pytest.raises(PbiRelationValidationError, match="cycle") as error:
        PbiRelationService(provider).apply(_request((PbiRelationDependency(2, 3),)))

    assert error.value.code == "dependency_cycle"
    assert provider.added_sub_issues == []
    assert provider.added_dependencies == []


def test_partial_sub_issue_failure_reports_confirmed_and_pending_edges() -> None:
    provider = FakeRelationProvider()
    provider.fail_sub_issue_id = 102
    request = _request((PbiRelationDependency(3, 2),))

    result = PbiRelationService(provider).apply(request)

    assert result.status == "incomplete"
    assert result.confirmed_children == (2,)
    assert result.pending_children == (3,)
    assert result.pending_dependencies == request.dependencies
    assert result.failure_code == "permission_denied"
    assert provider.added_dependencies == []


def test_sub_issue_readback_requires_both_parent_and_child_views() -> None:
    class InconsistentParentProvider(FakeRelationProvider):
        def get_pbi_parent_issue_number(
            self, repository: str, child_issue_number: int
        ) -> int | None:
            if child_issue_number == 2:
                return None
            return super().get_pbi_parent_issue_number(repository, child_issue_number)

    provider = InconsistentParentProvider()
    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.confirmed_children == (3,)
    assert result.pending_children == (2,)
    assert result.sub_issue_readback_complete is True
    assert result.failure_code == "relation_readback_incomplete"


def test_parent_endpoint_failure_preserves_other_child_confirmation() -> None:
    provider = FakeRelationProvider()
    provider.fail_parent_readback_number = 2

    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.confirmed_children == (3,)
    assert result.pending_children == (2,)
    assert result.sub_issue_readback_complete is False
    assert result.failure_code == "permission_denied"


def test_dependency_write_denial_reports_confirmed_children_and_pending_edge() -> None:
    provider = FakeRelationProvider()
    provider.fail_dependency = True
    request = _request((PbiRelationDependency(3, 2),))

    result = PbiRelationService(provider).apply(request)

    assert result.status == "incomplete"
    assert result.confirmed_children == (2, 3)
    assert result.confirmed_dependencies == ()
    assert result.pending_dependencies == request.dependencies
    assert result.failure_code == "permission_denied"


def test_dependency_readback_failure_is_incomplete_and_redacted() -> None:
    provider = FakeRelationProvider()
    provider.fail_sub_issue_id = 102
    provider.fail_blocked_by_call = (3, 2)
    result = PbiRelationService(provider).apply(
        _request((PbiRelationDependency(3, 2),))
    )

    payload = result.as_dict()
    assert result.status == "incomplete"
    assert result.dependency_readback_complete is False
    assert result.pending_dependencies == (PbiRelationDependency(3, 2),)
    assert "private-token" not in str(payload)


def test_incomplete_dependency_graph_fails_closed_before_writes() -> None:
    provider = FakeRelationProvider()
    provider.fail_blocking = True

    result = PbiRelationService(provider).apply(_request())

    assert result.status == "incomplete"
    assert result.pending_step == "preflight"
    assert result.failure_code == "permission_denied"
    assert provider.added_sub_issues == []
    assert provider.added_dependencies == []


def test_cross_repository_reference_is_rejected_before_provider_read() -> None:
    provider = FakeRelationProvider()
    request = _request()
    request = replace(
        request,
        children=(replace(request.children[0], repository="owner/other"),),
    )

    with pytest.raises(PbiRelationValidationError) as error:
        PbiRelationService(provider).apply(request)

    assert error.value.code == "cross_repository_child"
    assert provider.preflight_calls == 0


def test_duplicate_dependency_declarations_are_rejected_before_provider_read() -> None:
    provider = FakeRelationProvider()
    edge = PbiRelationDependency(3, 2)

    with pytest.raises(PbiRelationValidationError) as error:
        PbiRelationService(provider).apply(_request((edge, edge)))

    assert error.value.code == "duplicate_dependency"
    assert provider.preflight_calls == 0
