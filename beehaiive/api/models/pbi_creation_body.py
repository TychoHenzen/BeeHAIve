from ._common import BaseModel, Field

__all__ = ["PbiCreationBody"]


class PbiCreationBody(BaseModel):
    repository: str = Field(min_length=3, max_length=300)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=65_536)
    labels: list[str] = Field(default_factory=list, max_length=20)
