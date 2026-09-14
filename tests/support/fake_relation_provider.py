from __future__ import annotations

from beehaiive.pbi_relations import (
    PbiRelationIssue,
    PbiRelationProviderError,
    PbiRelationRequest,
    PbiRelationSnapshot,
)


def _fact(number: int, *, title: str | None = None) -> PbiRelationIssue:
    return PbiRelationIssue(
        100 + number,
        f"node-{number}",
        number,
        f"https://github.com/owner/repo/issues/{number}",
        title or f"Issue {number}",
        "OPEN",
    )


class FakeRelationProvider:
    def __init__(self) -> None:
        self.parent_children: set[int] = set()
        self.parent_by_child: dict[int, int | None] = {2: None, 3: None}
        self.blocked_by: dict[int, set[int]] = {2: set(), 3: set()}
        self.added_sub_issues: list[tuple[int, int]] = []
        self.added_dependencies: list[tuple[int, int]] = []
        self.preflight_calls = 0
        self.fail_blocking = False
        self.fail_sub_issue_id: int | None = None
        self.fail_parent_readback_number: int | None = None
        self.fail_blocked_by_call: tuple[int, int] | None = None
        self.fail_dependency = False
        self.blocked_by_calls: dict[int, int] = {}

    def prepare_pbi_relations(self, request: PbiRelationRequest) -> PbiRelationSnapshot:
        self.preflight_calls += 1
        return PbiRelationSnapshot(
            _fact(1),
            tuple(_fact(child.number) for child in request.children),
            tuple(_fact(number) for number in sorted(self.parent_children)),
            dict(self.parent_by_child),
        )

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        assert parent_issue_number == 1
        return tuple(_fact(number) for number in sorted(self.parent_children))

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None:
        assert repository == "owner/repo"
        if child_issue_number == self.fail_parent_readback_number:
            raise PbiRelationProviderError("permission_denied")
        return self.parent_by_child[child_issue_number]

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        self.blocked_by_calls[issue_number] = (
            self.blocked_by_calls.get(issue_number, 0) + 1
        )
        if self.fail_blocked_by_call == (
            issue_number,
            self.blocked_by_calls[issue_number],
        ):
            raise PbiRelationProviderError("permission_denied")
        return tuple(_fact(number) for number in sorted(self.blocked_by[issue_number]))

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        assert repository == "owner/repo"
        if self.fail_blocking:
            raise PbiRelationProviderError("permission_denied")
        return tuple(
            _fact(blocked)
            for blocked, blockers in self.blocked_by.items()
            if issue_number in blockers
        )

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        assert repository == "owner/repo"
        child_number = child_issue_id - 100
        self.added_sub_issues.append((parent_issue_number, child_issue_id))
        self.parent_children.add(child_number)
        self.parent_by_child[child_number] = parent_issue_number
        if child_issue_id == self.fail_sub_issue_id:
            raise PbiRelationProviderError("permission_denied")

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        assert repository == "owner/repo"
        blocker_number = blocker_issue_id - 100
        self.added_dependencies.append((blocked_issue_number, blocker_issue_id))
        if self.fail_dependency:
            raise PbiRelationProviderError("permission_denied")
        self.blocked_by[blocked_issue_number].add(blocker_number)
