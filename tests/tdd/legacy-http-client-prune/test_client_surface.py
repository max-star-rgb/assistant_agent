from importlib import import_module

import pytest


def test_retired_product_http_client_is_not_packaged() -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        import_module("assistant_agent.clients.http_agent")

    assert caught.value.name in {
        "assistant_agent.clients",
        "assistant_agent.clients.http_agent",
    }
