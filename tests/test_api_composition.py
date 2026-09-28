from dataclasses import MISSING, fields
from typing import Any

import pytest

from beehaiive.api.context import ApiRuntime


def test_api_runtime_rejects_missing_required_service_at_construction() -> None:
    required_values: dict[str, Any] = {
        field.name: object()
        for field in fields(ApiRuntime)
        if field.default is MISSING and field.default_factory is MISSING
    }
    required_values.pop("orchestrator")

    with pytest.raises(TypeError, match="orchestrator"):
        ApiRuntime(**required_values)
