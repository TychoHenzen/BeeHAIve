from ._common import BaseModel

__all__ = ["HandoffRequest"]


class HandoffRequest(BaseModel):
    branch: str
    base_branch: str | None = None
    body: str = ""
