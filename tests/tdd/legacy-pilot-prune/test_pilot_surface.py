from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "assistant_agent.api.identity",
        "assistant_agent.api.trial_access",
        "assistant_agent.multi_agent.agent_pilot_readiness",
    ],
)
def test_retired_pilot_module_is_not_packaged(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        import_module(module_name)

    assert caught.value.name == module_name


def test_retained_multi_agent_router_is_still_packaged() -> None:
    assert import_module("assistant_agent.multi_agent.agent_router")
