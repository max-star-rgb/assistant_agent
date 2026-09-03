from importlib import import_module

import pytest


def test_disconnected_runtime_contracts_are_absent() -> None:
    for module_name in (
        "assistant_agent.runtime.cancellation",
        "assistant_agent.runtime.capability_grants",
    ):
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(module_name)
        assert exc_info.value.name == module_name
