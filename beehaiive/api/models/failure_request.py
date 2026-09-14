from ._common import BaseModel, Field

__all__ = ["FailureRequest"]


class FailureRequest(BaseModel):
    error: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    recursive_spawn_depth: int = Field(default=0, ge=0)
