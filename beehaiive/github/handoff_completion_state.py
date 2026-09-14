from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beehaiive.github.errors.outcome_unknown_error import (
    GitHubOutcomeUnknownError as GitHubOutcomeUnknownError,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.errors.rate_limit_error import (
    GitHubRateLimitError as GitHubRateLimitError,
)
from beehaiive.github.graphql_helpers import _commit_oid as _commit_oid
from beehaiive.github.graphql_helpers import _mapping as _mapping
from beehaiive.github.graphql_helpers import _next_cursor as _next_cursor
from beehaiive.github.graphql_helpers import _nodes as _nodes
from beehaiive.github.graphql_helpers import _owner_query as _owner_query
from beehaiive.github.graphql_helpers import _project as _project
from beehaiive.github.queries.completion import BRANCH_REF_QUERY as BRANCH_REF_QUERY
from beehaiive.github.queries.completion import (
    COMPLETION_PROJECT_QUERY as COMPLETION_PROJECT_QUERY,
)
from beehaiive.github.queries.completion import (
    ISSUE_COMPLETION_QUERY as ISSUE_COMPLETION_QUERY,
)
from beehaiive.models import HandoffRequest


class HandoffCompletionStateMixin:
    def _issue_completion_state(
        self: Any, request: HandoffRequest
    ) -> Mapping[str, object]:
        owner, name = self._repository_parts(request.repository)
        data = self._client.execute(
            ISSUE_COMPLETION_QUERY,
            {"owner": owner, "name": name, "number": request.pbi_number},
        )
        raw_issue = _mapping(data.get("repository")).get("issue")
        if raw_issue is None:
            return {"valid": False, "error": "The linked issue no longer exists"}
        issue = _mapping(raw_issue)
        issue_id = issue.get("id")
        number = issue.get("number")
        state = issue.get("state")
        valid = (
            isinstance(issue_id, str)
            and bool(issue_id)
            and type(number) is int
            and number == request.pbi_number
            and state in {"OPEN", "CLOSED"}
        )
        return {
            "valid": valid,
            "error": "GitHub returned conflicting issue identity or state",
            "issue_id": issue_id if isinstance(issue_id, str) else "",
            "state": state if isinstance(state, str) else "",
        }

    def _source_ref_completion_state(
        self: Any, request: HandoffRequest, expected_head: str
    ) -> Mapping[str, object]:
        owner, name = self._repository_parts(request.repository)
        qualified_name = f"refs/heads/{request.branch}"
        data = self._client.execute(
            BRANCH_REF_QUERY,
            {"owner": owner, "name": name, "qualifiedName": qualified_name},
        )
        repository = _mapping(data.get("repository"))
        repository_id = repository.get("id")
        if not isinstance(repository_id, str) or not repository_id:
            return {
                "valid": False,
                "error": "GitHub omitted source repository identity",
            }
        if "ref" not in repository:
            return {"valid": False, "error": "GitHub omitted the source ref"}
        raw_ref = repository.get("ref")
        if raw_ref is None:
            return {"valid": True, "exists": False, "repository_id": repository_id}
        ref = _mapping(raw_ref)
        ref_id = ref.get("id")
        ref_name = ref.get("name")
        oid = _commit_oid(_mapping(ref.get("target")).get("oid"))
        if (
            not isinstance(ref_id, str)
            or not ref_id
            or ref_name != qualified_name
            or oid is None
        ):
            return {
                "valid": False,
                "error": "GitHub returned conflicting source ref identity",
            }
        return {
            "valid": True,
            "exists": True,
            "repository_id": repository_id,
            "ref_id": ref_id,
            "ref_name": qualified_name,
            "head_sha": oid,
            "expected_head": expected_head,
        }

    def _project_completion_state(
        self: Any, request: HandoffRequest
    ) -> Mapping[str, object]:
        if request.project_id != self.project_id:
            return {"valid": False, "error": "The handoff belongs to another Project"}
        cursor: str | None = None
        matching_items: list[Mapping[str, object]] = []
        status_field: Mapping[str, object] | None = None
        done_option_id: str | None = None
        project_id: str | None = None
        while True:
            data = self._client.execute(
                _owner_query(COMPLETION_PROJECT_QUERY, self.owner_type),
                {"owner": self.owner, "number": self.project_number, "cursor": cursor},
            )
            project = _project(data, self.owner_type)
            current_project_id = project.get("id")
            if not isinstance(current_project_id, str) or not current_project_id:
                return {"valid": False, "error": "GitHub omitted Project identity"}
            if project_id is not None and current_project_id != project_id:
                return {
                    "valid": False,
                    "error": "GitHub returned conflicting Project identity",
                }
            project_id = current_project_id

            fields = [
                field
                for field in _nodes(project.get("fields", {}))
                if field.get("name") == "Status"
            ]
            if len(fields) != 1:
                return {"valid": False, "error": "Project must have one Status field"}
            status_field = fields[0]
            field_id = status_field.get("id")
            options = status_field.get("options")
            if not isinstance(field_id, str) or not isinstance(options, list):
                return {"valid": False, "error": "Project Status field is incomplete"}
            done_option_ids: list[str] = []
            for option in cast(list[object], options):
                if not isinstance(option, Mapping):
                    continue
                option_record = cast(Mapping[str, object], option)
                option_id = option_record.get("id")
                if option_record.get("name") == "Done" and isinstance(option_id, str):
                    done_option_ids.append(option_id)
            if len(done_option_ids) != 1:
                return {
                    "valid": False,
                    "error": "Project Status has no unique Done option",
                }
            done_option_id = done_option_ids[0]

            items = _mapping(project.get("items"))
            for raw_item in _nodes(items):
                content = _mapping(raw_item.get("content"))
                repository = _mapping(content.get("repository"))
                if (
                    content.get("__typename") == "Issue"
                    and type(content.get("number")) is int
                    and content.get("number") == request.pbi_number
                    and repository.get("nameWithOwner") == request.repository
                ):
                    values = [
                        value
                        for value in _nodes(raw_item.get("fieldValues", {}))
                        if _mapping(value.get("field")).get("id") == field_id
                    ]
                    if len(values) != 1:
                        matching_items.append({"valid": False})
                    else:
                        value = values[0]
                        matching_items.append(
                            {
                                "valid": True,
                                "item_id": raw_item.get("id"),
                                "status": value.get("name"),
                                "option_id": value.get("optionId"),
                            }
                        )
            has_next, cursor = _next_cursor(items)
            if not has_next:
                break

        if len(matching_items) != 1:
            return {"valid": False, "error": "Expected one exact Project issue item"}
        item = matching_items[0]
        item_id = item.get("item_id")
        field_id = status_field.get("id")
        if (
            item.get("valid") is not True
            or not isinstance(item_id, str)
            or not item_id
            or not isinstance(field_id, str)
        ):
            return {
                "valid": False,
                "error": "Project item Status evidence is incomplete",
            }
        return {
            "valid": True,
            "project_id": project_id or "",
            "item_id": item_id,
            "field_id": field_id,
            "status": item.get("status") if isinstance(item.get("status"), str) else "",
            "option_id": item.get("option_id")
            if isinstance(item.get("option_id"), str)
            else "",
            "done_option_id": done_option_id,
        }


__all__ = ["HandoffCompletionStateMixin"]
