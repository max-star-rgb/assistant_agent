"""Real-provider construction test for the web tool plugin."""

import pytest

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.web.plugin import WebToolPlugin, web_provider_ready


def test_web_plugin_builds_configured_real_provider_tools(
    real_plugin_context: ToolPluginContext,
) -> None:
    if not web_provider_ready(real_plugin_context.config):
        pytest.skip("no real web provider is configured")

    tools = WebToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert {tool.name for tool in tools} == {"web_fetch", "web_search"}
