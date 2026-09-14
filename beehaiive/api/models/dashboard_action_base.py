from ._common import BaseModel

__all__ = ["DashboardActionBase"]


class DashboardActionBase(BaseModel):
    approved: bool = False
