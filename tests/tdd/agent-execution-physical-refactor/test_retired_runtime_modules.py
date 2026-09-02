from __future__ import annotations

import importlib.util
from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    ["events", "hook_dispatch", "recovery", "system_prompt_policy"],
)
def test_retired_runtime_module_is_not_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.runtime.{module_name}"

    assert importlib.util.find_spec(qualified_name) is None


@pytest.mark.parametrize("module_name", ["hook_dispatch", "recovery"])
def test_observability_owned_module_is_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.observability.{module_name}"

    assert import_module(qualified_name) is not None
