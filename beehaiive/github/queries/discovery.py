from __future__ import annotations

PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      title
    }
  }
}
"""

REPOSITORIES_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      repositories(first: 100, after: $cursor) {
        nodes { nameWithOwner }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        nodes {
          content {
            __typename
            ... on Issue {
              number
              title
              url
              state
              stateReason
              repository { nameWithOwner }
              # Keep the project-wide query below GitHub's node limit. The
              # provider completes these connections with repository queries.
              labels(first: 20) {
                nodes { name }
                pageInfo { hasNextPage endCursor }
              }
              subIssues(first: 20) {
                nodes {
                  number
                  title
                  state
                  stateReason
                  labels(first: 20) {
                    nodes { name }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
              comments(first: 20) {
                nodes {
                  author { ... on User { login } ... on Bot { login } }
                  body
                  createdAt
                  url
                }
                pageInfo { hasNextPage endCursor }
              }
              closedByPullRequestsReferences(includeClosedPrs: true, first: 20) {
                nodes {
                  number
                  url
                  state
                  merged
                  headRefName
                  headRef { name }
                  isDraft
                  reviewDecision
                  reviewRequests(first: 20) {
                    nodes {
                      requestedReviewer {
                        ... on User { login }
                        ... on Team { name }
                      }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                  latestReviews(first: 20) {
                    nodes {
                      author { ... on User { login } ... on Bot { login } }
                      state
                      body
                      submittedAt
                      url
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
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

ISSUE_LABELS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      labels(first: 100, after: $cursor) {
        nodes { name }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $cursor) {
        nodes {
          number
          title
          state
          stateReason
          labels(first: 100) {
            nodes { name }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 50, after: $cursor) {
        nodes {
          author { ... on User { login } ... on Bot { login } }
          body
          createdAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_PULL_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(
        includeClosedPrs: true
        first: 100
        after: $cursor
      ) {
        nodes {
          number
          url
          state
          merged
          headRefName
          headRef { name target { oid } }
          isDraft
          reviewDecision
          reviewRequests(first: 100) {
            nodes {
              requestedReviewer {
                ... on User { login }
                ... on Team { name }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
          latestReviews(first: 100) {
            nodes {
              author { ... on User { login } ... on Bot { login } }
              state
              body
              submittedAt
              url
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

__all__ = [
    "PROJECT_QUERY",
    "REPOSITORIES_QUERY",
    "ITEMS_QUERY",
    "ISSUE_LABELS_QUERY",
    "ISSUE_SUB_ISSUES_QUERY",
    "ISSUE_COMMENTS_QUERY",
    "ISSUE_PULL_REQUESTS_QUERY",
]
