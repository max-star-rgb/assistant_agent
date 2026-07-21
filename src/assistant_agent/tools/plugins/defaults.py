"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPlugin, ToolPluginContext
from assistant_agent.tools.plugins.core.plugin import CoreToolPlugin
from assistant_agent.tools.plugins.durable_task.plugin import DurableTaskToolPlugin
from assistant_agent.tools.plugins.image_generation.plugin import ImageGenerationToolPlugin
from assistant_agent.tools.plugins.memory.plugin import MemoryToolPlugin
from assistant_agent.tools.plugins.personal_assistant.plugin import PersonalAssistantToolPlugin
from assistant_agent.tools.plugins.shopping.plugin import ShoppingToolPlugin
from assistant_agent.tools.plugins.vision.plugin import VisionToolPlugin
from assistant_agent.tools.plugins.web.plugin import WebToolPlugin


def default_tool_plugins() -> tuple[ToolPlugin, ...]:
    """Return the explicit, ordered set of trusted built-in capability plugins."""

    return (
        CoreToolPlugin(),
        MemoryToolPlugin(),
        VisionToolPlugin(),
        ShoppingToolPlugin(),
        PersonalAssistantToolPlugin(),
        WebToolPlugin(),
        ImageGenerationToolPlugin(),
        DurableTaskToolPlugin(),
    )


def build_default_tools(context: ToolPluginContext) -> list[Tool]:
    """Build default tools without allowing plugins to bypass registry governance."""

    tools: list[Tool] = []
    for plugin in default_tool_plugins():
        tools.extend(plugin.build_tools(context))
    return tools
