from __future__ import annotations


def _rest_issue(number: int, body: str = "") -> dict[str, object]:
    url = f"https://github.com/owner/repo/issues/{number}"
    return {
        "id": 100 + number,
        "node_id": f"node-{number}",
        "number": number,
        "url": f"https://api.github.com/repos/owner/repo/issues/{number}",
        "html_url": url,
        "title": f"Issue {number}",
        "state": "open",
        "body": body,
    }


class FakeRestArrayClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def request_rest(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | list[object]]:
        self.calls.append((method, path, payload))
        if method == "GET" and path.endswith("page=1"):
            return 200, [_rest_issue(number) for number in range(2, 102)]
        if method == "GET" and path.endswith("page=2"):
            return 200, [_rest_issue(102)]
        if method == "POST":
            return 201, {}
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        raise AssertionError(f"Unexpected GraphQL query: {query[:40]} {variables}")
