"""Real-mode construction test for the core tool plugin."""

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.core.plugin import CoreToolPlugin


def test_core_plugin_builds_local_tools_in_real_mode(
    real_plugin_context: ToolPluginContext,
) -> None:
    tools = CoreToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert {tool.name for tool in tools} == {"python_interpreter", "tool_search"}
