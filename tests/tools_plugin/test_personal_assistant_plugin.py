"""Real-provider construction test for the personal-assistant tool plugin."""

import pytest

from assistant_agent.services.personal_assistant_mcp_adapters import (
    configured_personal_assistant_tools,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.personal_assistant.plugin import (
    PersonalAssistantToolPlugin,
)


def test_personal_assistant_plugin_builds_configured_real_mcp_tools(
    real_plugin_context: ToolPluginContext,
) -> None:
    expected = set(
        configured_personal_assistant_tools(real_plugin_context.mcp_server_configs)
    )
    if not expected:
        pytest.skip("no real personal-assistant MCP tool mapping is configured")

    tools = PersonalAssistantToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert {tool.name for tool in tools} == expected
