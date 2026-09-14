from __future__ import annotations

PBI_REFINEMENT_ISSUE_QUERY = """
query PbiRefinementIssue($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id number url body state
      labels(first: 100) {
        nodes { name }
        pageInfo { hasNextPage endCursor }
      }
      subIssues(first: 100) {
        nodes { number title state }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

UPDATE_REFINEMENT_ISSUE_MUTATION = """
mutation UpdatePbiRefinementIssue($input: UpdateIssueInput!) {
  updateIssue(input: $input) { issue { id } }
}
"""

ADD_PROJECT_ITEM_MUTATION = """
mutation($input: AddProjectV2ItemByIdInput!) {
  addProjectV2ItemById(input: $input) { item { id } }
}
"""

ADD_ISSUE_LABELS_MUTATION = """
mutation($input: AddLabelsToLabelableInput!) {
  addLabelsToLabelable(input: $input) {
    labelable { ... on Issue { id labels(first: 100) { nodes { name } } } }
  }
}
"""

__all__ = [
    "PBI_REFINEMENT_ISSUE_QUERY",
    "UPDATE_REFINEMENT_ISSUE_MUTATION",
    "ADD_PROJECT_ITEM_MUTATION",
    "ADD_ISSUE_LABELS_MUTATION",
]
