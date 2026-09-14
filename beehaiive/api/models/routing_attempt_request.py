from typing import Literal

from ._common import BaseModel, Field

__all__ = ["RoutingAttemptRequest"]


class RoutingAttemptRequest(BaseModel):
    outcome: Literal["failure", "retry", "success"]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    failure_context: str = Field(default="", max_length=2_000)
    recursive_spawn_depth: int = Field(default=0, ge=0)
