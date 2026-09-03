from importlib import import_module

import pytest


def test_disconnected_observability_wrappers_are_absent() -> None:
    import_module("assistant_agent.media.vision.observability")

    for name in (
        "langsmith_config",
        "langsmith_native",
        "recovery",
        "trace_content_policy",
    ):
        module_name = f"assistant_agent.observability.{name}"
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(module_name)
        assert exc_info.value.name == module_name
