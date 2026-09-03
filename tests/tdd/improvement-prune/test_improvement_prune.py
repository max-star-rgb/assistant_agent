from importlib import import_module

import pytest


def test_retired_improvement_island_is_absent() -> None:
    import_module("assistant_agent.observability.trace_store")

    for module_name in (
        "assistant_agent.improvement",
        "assistant_agent.observability.trajectory_debug",
    ):
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(module_name)
        assert exc_info.value.name == module_name
