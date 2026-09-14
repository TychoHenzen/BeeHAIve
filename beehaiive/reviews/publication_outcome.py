from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .finding_publication_channel import FindingPublicationChannel
    from .finding_publication_state import FindingPublicationState


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    state: FindingPublicationState
    channel: FindingPublicationChannel | None = None
    remote_id: str | None = None
    remote_url: str | None = None
    retry_evidence: dict[str, object] | None = None
