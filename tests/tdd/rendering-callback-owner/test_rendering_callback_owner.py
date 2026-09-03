from importlib import import_module

import pytest


def test_rendering_callback_is_owned_by_agent_server() -> None:
    module = import_module("assistant_agent.agent_server.rendering_3d_callback")

    assert module.router.routes[0].path.endswith("/3d-gen-back")
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module("assistant_agent.api")
    assert exc_info.value.name == "assistant_agent.api"
