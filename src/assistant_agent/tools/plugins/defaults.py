"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tools.plugins.contracts import ToolPlugin
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
