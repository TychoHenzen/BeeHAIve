from dataclasses import MISSING, fields
from inspect import signature
from typing import Any

import pytest

from beehaiive.api.context import ApiRouteContext, ApiRuntime
from beehaiive.api.routes.system import register_routes as register_system_routes


def test_api_runtime_rejects_missing_required_service_at_construction() -> None:
    required_values: dict[str, Any] = {
        field.name: object()
        for field in fields(ApiRuntime)
        if field.default is MISSING and field.default_factory is MISSING
    }
    required_values.pop("orchestrator")

    with pytest.raises(TypeError, match="orchestrator"):
        ApiRuntime(**required_values)


def test_system_routes_use_typed_route_context() -> None:
    context_parameter = signature(register_system_routes).parameters["context"]

    assert context_parameter.annotation is ApiRouteContext
    assert not hasattr(ApiRouteContext, "as_legacy_mapping")
