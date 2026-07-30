"""Registration boundaries for the real-only unified shopping Tool."""

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.shopping.backend import (
    create_shopping_compare_adapter,
    create_shopping_search_adapter,
)
from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.registry_factory import create_default_registry


def test_mock_registry_does_not_register_shopping_tools() -> None:
    registry = create_default_registry(ProviderConfig(), plugin_modules=[])

    assert "shopping_search" not in registry.list()


def test_real_ready_plugin_registers_only_unified_shopping_search() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
        shopping_search_provider="haodanku",
        shopping_compare_provider="haodanku",
        haodanku_api_key="test-only",
    )

    tools = ShoppingToolPlugin().build_tools(
        ToolPluginContext(config=config, mcp_server_configs=[])
    )

    assert [tool.name for tool in tools] == ["shopping_search"]


def test_real_unconfigured_plugin_registers_no_shopping_tool() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
    )

    tools = ShoppingToolPlugin().build_tools(
        ToolPluginContext(config=config, mcp_server_configs=[])
    )

    assert tools == []


def test_mock_configuration_cannot_create_shopping_adapters() -> None:
    config = ProviderConfig(provider_mode="mock")

    with pytest.raises(ValueError, match="configured real shopping search"):
        create_shopping_search_adapter(config)
    with pytest.raises(ValueError, match="configured real shopping compare"):
        create_shopping_compare_adapter(config)


def test_shopping_tool_requires_explicit_adapters() -> None:
    with pytest.raises(TypeError):
        ShoppingSearchTool()
