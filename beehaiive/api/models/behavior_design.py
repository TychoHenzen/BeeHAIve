from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from beehaiive.behavior import MAX_BEHAVIOR_PROMPT_LENGTH


class BehaviorGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=MAX_BEHAVIOR_PROMPT_LENGTH)


class BehaviorSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    bindings: dict[str, Any] = Field(default_factory=dict)


class BehaviorBindingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: dict[str, Any]


class BehaviorAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=128)


__all__ = [
    "BehaviorAssignmentRequest",
    "BehaviorBindingsRequest",
    "BehaviorGenerateRequest",
    "BehaviorSaveRequest",
]
