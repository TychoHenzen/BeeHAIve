from __future__ import annotations

PULL_REQUEST_CHECKS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      headRef { target { oid } }
      statusCheckRollup {
        commit { oid }
        state
        contexts(first: 100, after: $cursor) {
          nodes {
            __typename
            ... on CheckRun {
              name
              status
              conclusion
              isRequired(pullRequestNumber: $number)
              detailsUrl
              startedAt
              completedAt
            }
            ... on StatusContext {
              context
              state
              isRequired(pullRequestNumber: $number)
              targetUrl
              createdAt
              updatedAt
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""

PULL_REQUEST_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      url
      state
      merged
      headRefName
      headRepository { nameWithOwner }
      headRef { name target { oid } }
      baseRefName
      baseRef { name target { oid } }
      mergeable
      mergeStateStatus
      isDraft
      mergeCommit { oid }
    }
  }
}
"""

ISSUE_COMPLETION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { id number state }
  }
}
"""

CLOSE_ISSUE_MUTATION = """
mutation($input: CloseIssueInput!) {
  closeIssue(input: $input) { issue { id number state } }
}
"""

BRANCH_REF_QUERY = """
query($owner: String!, $name: String!, $qualifiedName: String!) {
  repository(owner: $owner, name: $name) {
    id
    ref(qualifiedName: $qualifiedName) { id name target { oid } }
  }
}
"""

UPDATE_REFS_MUTATION = """
mutation($input: UpdateRefsInput!) {
  updateRefs(input: $input) { clientMutationId }
}
"""

COMPLETION_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name options { id name }
          }
        }
      }
      items(first: 100, after: $cursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue { number repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name optionId
                field { ... on ProjectV2FieldCommon { id name } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

UPDATE_PROJECT_STATUS_MUTATION = """
mutation($input: UpdateProjectV2ItemFieldValueInput!) {
  updateProjectV2ItemFieldValue(input: $input) {
    projectV2Item { id }
  }
}
"""

__all__ = [
    "PULL_REQUEST_CHECKS_QUERY",
    "PULL_REQUEST_STATE_QUERY",
    "ISSUE_COMPLETION_QUERY",
    "CLOSE_ISSUE_MUTATION",
    "BRANCH_REF_QUERY",
    "UPDATE_REFS_MUTATION",
    "COMPLETION_PROJECT_QUERY",
    "UPDATE_PROJECT_STATUS_MUTATION",
]
