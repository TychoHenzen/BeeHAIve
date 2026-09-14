def make_rollup(*contexts: dict[str, object], oid: str = "abc") -> dict[str, object]:
    return {
        "commit": {"oid": oid},
        "state": "SUCCESS",
        "contexts": {"nodes": list(contexts)},
    }
