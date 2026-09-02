from __future__ import annotations

import importlib.util

import pytest


@pytest.mark.parametrize("module_name", ["events", "system_prompt_policy"])
def test_retired_runtime_module_is_not_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.runtime.{module_name}"

    assert importlib.util.find_spec(qualified_name) is None
