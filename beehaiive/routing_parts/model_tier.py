from __future__ import annotations

from enum import StrEnum


class ModelTier(StrEnum):
    """Available model tiers, ordered from routine work to human review."""

    LUNA = "luna"
    TERRA = "terra"
    SOL = "sol"
    ASTRA = "astra"
    HUMAN = "human"
