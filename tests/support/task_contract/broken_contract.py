from beehaiive.contracts import (
    ContractError,
)


class BrokenContract:
    def as_dict(self) -> dict[str, object]:
        raise ContractError("broken contract")
