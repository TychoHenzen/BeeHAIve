from __future__ import annotations

PBI_RELATION_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $repositoryCursor: String, $itemCursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      repositories(first: 100, after: $repositoryCursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
      fields(first: 100) {
        nodes { ... on ProjectV2SingleSelectField { id name } }
      }
      items(first: 100, after: $itemCursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue { id number repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { id name } }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

__all__ = ["PBI_RELATION_PROJECT_QUERY"]
