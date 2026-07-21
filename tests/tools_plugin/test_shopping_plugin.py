"""Real-provider construction test for the shopping tool plugin."""

import pytest

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.shopping.plugin import (
    ShoppingToolPlugin,
    shopping_provider_ready,
)


def test_shopping_plugin_builds_configured_real_provider_tool(
    real_plugin_context: ToolPluginContext,
) -> None:
    if not shopping_provider_ready(real_plugin_context.config):
        pytest.skip("no real shopping provider is configured")

    tools = ShoppingToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert [tool.name for tool in tools] == ["shopping_search"]
