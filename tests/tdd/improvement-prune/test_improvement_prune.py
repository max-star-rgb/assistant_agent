from importlib import import_module

import pytest


def test_retired_improvement_island_is_absent() -> None:
    for module_name in (
        "assistant_agent.improvement",
        "assistant_agent.observability.trajectory_debug",
        "assistant_agent.observability.trace_store",
    ):
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(module_name)
        expected_missing = {module_name}
        if module_name.startswith("assistant_agent.observability."):
            expected_missing.add("assistant_agent.observability")
        assert exc_info.value.name in expected_missing
