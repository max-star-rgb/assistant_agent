from importlib import import_module

import pytest


def test_token_counter_is_owned_by_native_agent() -> None:
    module = import_module("assistant_agent.native_agent.token_counter")

    assert module.TokenizerJsonTokenCounter
    assert not hasattr(module.TokenizerJsonTokenCounter, "count_chat_request")
    assert not hasattr(module, "create_visual_context_token_counter")
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module("assistant_agent.context.token_counter")
    assert exc_info.value.name == "assistant_agent.context.token_counter"
