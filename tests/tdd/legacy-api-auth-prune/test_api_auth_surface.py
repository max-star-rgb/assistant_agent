from importlib import import_module

import pytest


def test_retired_fastapi_auth_dependency_is_not_packaged() -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        import_module("assistant_agent.api.auth")

    assert caught.value.name == "assistant_agent.api.auth"
