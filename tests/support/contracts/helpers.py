from beehaiive.contracts import (
    ArtifactRequirement,
    TaskContract,
    TaskOutcome,
)


def contract(*, artifact: bool = False) -> TaskContract:
    return TaskContract(
        "test.contract",
        1,
        "inspect",
        {"repository": "owner/api"},
        ("read_repository",),
        (ArtifactRequirement("report"),) if artifact else (),
        tuple(TaskOutcome),
        ("summary",),
    )


def nested_value(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value
