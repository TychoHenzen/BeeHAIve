from __future__ import annotations

import math
import os
from threading import Lock

from beehaiive.github.constants import (
    DEFAULT_DISCOVERY_CACHE_SECONDS as DEFAULT_DISCOVERY_CACHE_SECONDS,
)
from beehaiive.github.discovery_helpers import (
    _discovery_cache_seconds_from_environment,
)
from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_protocol import GraphQLClient as GraphQLClient
from beehaiive.github.transport import UrllibGraphQLClient as UrllibGraphQLClient
from beehaiive.models import ProjectSnapshot

from .discovery import DiscoveryMixin
from .handoff_complete import HandoffCompleteMixin
from .handoff_completion_state import HandoffCompletionStateMixin
from .handoff_create import HandoffCreateMixin
from .handoff_ensure import HandoffEnsureMixin
from .handoff_lookup import HandoffLookupMixin
from .handoff_merge import HandoffMergeMixin
from .handoff_merge_state import HandoffMergeStateMixin
from .handoff_poll import HandoffPollMixin
from .handoff_result import HandoffResultMixin
from .metadata import MetadataMixin
from .pbi_creation_prepare import PbiCreationPrepareMixin
from .pbi_creation_state import PbiCreationStateMixin
from .pbi_creation_write import PbiCreationWriteMixin
from .pbi_refinement_apply import PbiRefinementApplyMixin
from .pbi_refinement_project import PbiRefinementProjectMixin
from .pbi_refinement_target import PbiRefinementTargetMixin
from .pbi_relations_lookup import PbiRelationsLookupMixin
from .pbi_relations_mutation import PbiRelationsMutationMixin
from .pbi_relations_prepare import PbiRelationsPrepareMixin


class GitHubProjectProvider(
    MetadataMixin,
    DiscoveryMixin,
    PbiCreationPrepareMixin,
    PbiCreationStateMixin,
    PbiCreationWriteMixin,
    PbiRefinementTargetMixin,
    PbiRefinementProjectMixin,
    PbiRefinementApplyMixin,
    PbiRelationsPrepareMixin,
    PbiRelationsLookupMixin,
    PbiRelationsMutationMixin,
    HandoffLookupMixin,
    HandoffMergeStateMixin,
    HandoffCompletionStateMixin,
    HandoffEnsureMixin,
    HandoffMergeMixin,
    HandoffPollMixin,
    HandoffCompleteMixin,
    HandoffCreateMixin,
    HandoffResultMixin,
):
    def __init__(
        self,
        owner: str,
        project_number: int,
        token: str,
        client: GraphQLClient | None = None,
        endpoint: str = "https://api.github.com/graphql",
        owner_type: str = "user",
        discovery_cache_seconds: float = DEFAULT_DISCOVERY_CACHE_SECONDS,
    ) -> None:
        if owner_type not in {"user", "organization"}:
            raise ProviderError(
                "GitHub Project owner type must be user or organization"
            )
        if not math.isfinite(discovery_cache_seconds) or discovery_cache_seconds < 0:
            raise ProviderError(
                "GitHub discovery cache seconds must be a finite non-negative number"
            )
        self.owner = owner
        self.project_number = project_number
        self.owner_type = owner_type
        self.project_id = f"{owner}:{project_number}"
        self._client = client or UrllibGraphQLClient(token, endpoint)
        self._discovery_cache_seconds = discovery_cache_seconds
        self._discovery_cache: tuple[float, ProjectSnapshot] | None = None
        self._discovery_lock = Lock()

    @classmethod
    def from_environment(cls) -> GitHubProjectProvider:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        owner = os.environ.get("GITHUB_PROJECT_OWNER")
        number_text = os.environ.get("GITHUB_PROJECT_NUMBER")
        owner_type = os.environ.get("GITHUB_PROJECT_OWNER_TYPE", "user")
        if not token or not owner or not number_text:
            raise ProviderError(
                "Set GITHUB_TOKEN, GITHUB_PROJECT_OWNER, and GITHUB_PROJECT_NUMBER"
            )
        try:
            number = int(number_text)
        except ValueError as exc:
            raise ProviderError("GITHUB_PROJECT_NUMBER must be an integer") from exc
        return cls(
            owner,
            number,
            token,
            owner_type=owner_type,
            discovery_cache_seconds=_discovery_cache_seconds_from_environment(),
        )


__all__ = ["GitHubProjectProvider"]
