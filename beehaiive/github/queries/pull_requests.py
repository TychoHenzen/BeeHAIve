from __future__ import annotations

PULL_REQUEST_REVIEW_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100, after: $cursor) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Team { name }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_REQUEST_LATEST_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      latestReviews(first: 100, after: $cursor) {
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
  }
}
"""

REPOSITORY_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedBranch: String!
  $pullRequestCursor: String
) {
  repository(owner: $owner, name: $name) {
    id
    defaultBranchRef { name target { oid } }
    ref(qualifiedName: $qualifiedBranch) { name target { oid } }
    pullRequests(
      first: 100
      after: $pullRequestCursor
      states: [OPEN, CLOSED, MERGED]
    ) {
      nodes {
        id number url title body state isDraft
        headRefName headRefOid baseRefName
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

DEFAULT_BRANCH_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
  }
}
"""

BASE_BRANCH_QUERY = """
query($owner: String!, $name: String!, $qualifiedBranch: String!) {
  repository(owner: $owner, name: $name) {
    baseRef: ref(qualifiedName: $qualifiedBranch) { name target { oid } }
  }
}
"""

CREATE_REF_MUTATION = """
mutation($input: CreateRefInput!) {
  createRef(input: $input) { ref { name target { oid } } }
}
"""

CREATE_PULL_REQUEST_MUTATION = """
mutation($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest {
      id number url title body state isDraft
      headRefName headRefOid baseRefName
    }
  }
}
"""

UPDATE_PULL_REQUEST_MUTATION = """
mutation($input: UpdatePullRequestInput!) {
  updatePullRequest(input: $input) {
    pullRequest {
      id number url title body state isDraft
      headRefName headRefOid baseRefName
    }
  }
}
"""

__all__ = [
    "PULL_REQUEST_REVIEW_REQUESTS_QUERY",
    "PULL_REQUEST_LATEST_REVIEWS_QUERY",
    "REPOSITORY_QUERY",
    "DEFAULT_BRANCH_QUERY",
    "BASE_BRANCH_QUERY",
    "CREATE_REF_MUTATION",
    "CREATE_PULL_REQUEST_MUTATION",
    "UPDATE_PULL_REQUEST_MUTATION",
]
