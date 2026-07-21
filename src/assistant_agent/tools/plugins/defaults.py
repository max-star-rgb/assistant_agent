"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPlugin, ToolPluginContext
from assistant_agent.tools.plugins.core import CoreToolPlugin
from assistant_agent.tools.plugins.durable_task import DurableTaskToolPlugin
from assistant_agent.tools.plugins.image_generation import ImageGenerationToolPlugin
from assistant_agent.tools.plugins.memory import MemoryToolPlugin
from assistant_agent.tools.plugins.personal_assistant import PersonalAssistantToolPlugin
from assistant_agent.tools.plugins.shopping import ShoppingToolPlugin
from assistant_agent.tools.plugins.vision import VisionToolPlugin
from assistant_agent.tools.plugins.web import WebToolPlugin


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
