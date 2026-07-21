"""Real-mode construction test for the memory tool plugin."""

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.memory.plugin import MemoryToolPlugin


def test_memory_plugin_builds_memory_tools_in_real_mode(
    real_plugin_context: ToolPluginContext,
) -> None:
    tools = MemoryToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert {tool.name for tool in tools} == {
        "memory_ingest_status",
        "memory_media_ingest",
        "memory_retrieval",
        "memory_save",
    }
