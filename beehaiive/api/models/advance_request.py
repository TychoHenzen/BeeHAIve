from ._common import BaseModel, Stage

__all__ = ["AdvanceRequest"]


class AdvanceRequest(BaseModel):
    target: Stage
