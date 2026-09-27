from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from beehaiive.building_signal import MAX_BUILDING_SIGNAL_PROMPT_LENGTH


class BuildingSignalGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=MAX_BUILDING_SIGNAL_PROMPT_LENGTH)


class BuildingSignalRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: dict[str, Any]


class BuildingSignalAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    building_id: str = Field(min_length=1, max_length=128)


__all__ = [
    "BuildingSignalAssignmentRequest",
    "BuildingSignalGenerateRequest",
    "BuildingSignalRuleRequest",
]
