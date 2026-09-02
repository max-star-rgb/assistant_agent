from __future__ import annotations

import importlib.util

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "compaction",
        "compactor",
        "conversation",
        "finalization",
        "policy",
        "soul_source",
        "sources",
        "user_profile_source",
    ],
)
def test_retired_context_module_is_not_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.context.{module_name}"

    assert importlib.util.find_spec(qualified_name) is None


@pytest.mark.parametrize("module_name", ["models", "report", "token_budget", "token_counter"])
def test_live_context_module_remains_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.context.{module_name}"

    assert importlib.util.find_spec(qualified_name) is not None
