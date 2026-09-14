from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot


def snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)),),
    )
