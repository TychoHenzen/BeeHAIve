import re

PULL_REQUEST_REVIEW_SNAPSHOT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      url
      state
      merged
      isDraft
      headRef { target { oid } }
      reviews(first: 100) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
          state
          body
          submittedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { id login name }
            ... on Team { id name slug }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          startDiffSide
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              id
              author { __typename ... on User { id login } ... on Bot { id login } }
              body
              createdAt
              updatedAt
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


PULL_REQUEST_REVIEWS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
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


PULL_REQUEST_REVIEW_REQUESTS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100, after: $cursor) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { id login name }
            ... on Team { id name slug }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


PULL_REQUEST_REVIEW_THREADS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          startDiffSide
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              id
              author { __typename ... on User { id login } ... on Bot { id login } }
              body
              createdAt
              updatedAt
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


PULL_REQUEST_THREAD_COMMENTS_PAGE_QUERY = """
query($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 20, after: $cursor) {
        nodes {
          id
          author { __typename ... on User { id login } ... on Bot { id login } }
          body
          createdAt
          updatedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


PUBLISH_PULL_REQUEST_REVIEW_MUTATION = """
mutation($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) {
    pullRequestReview { id url }
  }
}
"""


PULL_REQUEST_ID_MARKER = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repository>[A-Za-z0-9_.-]{1,100})#(?P<number>[1-9][0-9]{0,9})"
)
