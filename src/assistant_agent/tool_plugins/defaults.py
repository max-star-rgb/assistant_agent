"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tool_plugins.contracts import ToolPlugin
from assistant_agent.tool_plugins.builtin.durable_task.plugin import DurableTaskToolPlugin
from assistant_agent.tool_plugins.builtin.image_generation.plugin import ImageGenerationToolPlugin
from assistant_agent.tool_plugins.builtin.local_file_access.plugin import (
    LocalFileAccessPlugin,
)
from assistant_agent.tool_plugins.builtin.personal_assistant_mcp.plugin import (
    PersonalAssistantMCPToolPlugin,
)
from assistant_agent.tool_plugins.builtin.python_execution.plugin import (
    PythonExecutionPlugin,
)
from assistant_agent.tool_plugins.builtin.shopping.plugin import ShoppingToolPlugin
from assistant_agent.tool_plugins.builtin.tool_discovery.plugin import (
    ToolDiscoveryPlugin,
)
from assistant_agent.tool_plugins.builtin.vision_understanding.plugin import (
    VisionUnderstandingPlugin,
)
from assistant_agent.tool_plugins.builtin.visual_image_search.plugin import (
    VisualImageSearchPlugin,
)
from assistant_agent.tool_plugins.builtin.web_access.plugin import WebAccessToolPlugin


def default_tool_plugins() -> tuple[ToolPlugin, ...]:
    """Return the explicit, ordered set of trusted built-in capability plugins."""

    return (
        LocalFileAccessPlugin(),
        ToolDiscoveryPlugin(),
        PythonExecutionPlugin(),
        VisionUnderstandingPlugin(),
        VisualImageSearchPlugin(),
        ShoppingToolPlugin(),
        PersonalAssistantMCPToolPlugin(),
        WebAccessToolPlugin(),
        ImageGenerationToolPlugin(),
        DurableTaskToolPlugin(),
    )
