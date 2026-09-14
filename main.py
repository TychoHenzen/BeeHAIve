import collections.abc as _collections_abc
import dataclasses as _dataclasses
import os
import pathlib as _pathlib
import secrets as _secrets
import typing as _typing
from pathlib import Path
from typing import Any

import fastapi as _fastapi
import fastapi.exception_handlers as _fastapi_exception_handlers
import fastapi.exceptions as _fastapi_exceptions
import fastapi.responses as _fastapi_responses
from fastapi import HTTPException as HTTPException

from beehaiive import agent as _agent
from beehaiive import conflict_repair as _conflict_repair
from beehaiive import dashboard as _dashboard
from beehaiive import meta_review as _meta_review
from beehaiive import pbi_creation as _pbi_creation
from beehaiive import pbi_refinement_mutation as _pbi_refinement_mutation
from beehaiive import pbi_relations as _pbi_relations
from beehaiive import provider as _provider
from beehaiive import quality_gates as _quality_gates
from beehaiive import review as _review
from beehaiive import review_github as _review_github
from beehaiive import review_repair as _review_repair
from beehaiive import routing as _routing
from beehaiive import scheduler as _scheduler
from beehaiive import storage as _storage
from beehaiive import workflow as _workflow
from beehaiive.agent import DEFAULT_DEMO_TASK as DEFAULT_DEMO_TASK
from beehaiive.agent import DEMO_TASK_NAME as DEMO_TASK_NAME
from beehaiive.api import models as _api_models
from beehaiive.api.app import create_app as create_app
from beehaiive.api.helpers import configuration as _configuration
from beehaiive.api.helpers import dashboard as _dashboard_helpers
from beehaiive.api.helpers import http as _http_helpers
from beehaiive.api.helpers import serialization as _serialization_helpers
from beehaiive.quality_gates import RepositoryGateSuite
from beehaiive.workflow import Constitution, WorkflowService, WorkflowStore

_os = os

_COMPAT_MODULES = (
    _api_models,
    _configuration,
    _dashboard_helpers,
    _http_helpers,
    _serialization_helpers,
    _agent,
    _conflict_repair,
    _dashboard,
    _meta_review,
    _pbi_creation,
    _pbi_refinement_mutation,
    _pbi_relations,
    _provider,
    _quality_gates,
    _review,
    _review_github,
    _review_repair,
    _routing,
    _scheduler,
    _storage,
    _workflow,
    _fastapi,
    _fastapi_exception_handlers,
    _fastapi_exceptions,
    _fastapi_responses,
    _collections_abc,
    _dataclasses,
    _os,
    _pathlib,
    _secrets,
    _typing,
)


def __getattr__(name: str) -> Any:
    for module in _COMPAT_MODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _production_workflow_service() -> WorkflowService:
    repository = Path(
        os.environ.get("BEEHAIIVE_WORKFLOW_REPOSITORY", str(Path(__file__).parent))
    )
    database = os.environ.get(
        "BEEHAIIVE_WORKFLOW_DB",
        str(repository / ".beehaiive" / "workflow.db"),
    )
    constitution_path = Path(__file__).parent / "constitution.json"
    return WorkflowService(
        WorkflowStore(database),
        repository,
        Constitution.load(constitution_path),
        None,
        check_runner=RepositoryGateSuite(),
    )


app = (
    None
    if os.environ.get("BEEHAIIVE_SKIP_PRODUCTION_APP") == "1"
    else create_app(
        workflow_service=_production_workflow_service(),
        workflow_actor=os.environ.get("BEEHAIIVE_WORKFLOW_ACTOR"),
        require_review_adapters=True,
    )
)
