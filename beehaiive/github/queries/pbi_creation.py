from __future__ import annotations

PBI_CREATION_TARGET_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      repositories(first: 100, after: $cursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name options { id name }
          }
        }
      }
    }
  }
}
"""

PBI_CREATION_LABELS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
    labels(first: 100, after: $cursor) {
      nodes { id name description }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PBI_CREATION_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id number url title body state
      labels(first: 100) { nodes { name } }
    }
  }
}
"""

PBI_CREATION_PROJECT_QUERY = """
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
            ... on Issue { id number repository { nameWithOwner } }
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

__all__ = [
    "PBI_CREATION_TARGET_QUERY",
    "PBI_CREATION_LABELS_QUERY",
    "PBI_CREATION_ISSUE_QUERY",
    "PBI_CREATION_PROJECT_QUERY",
]
