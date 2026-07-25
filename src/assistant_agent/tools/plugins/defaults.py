"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tools.plugins.contracts import ToolPlugin
from assistant_agent.tools.plugins.builtin.durable_task.plugin import DurableTaskToolPlugin
from assistant_agent.tools.plugins.builtin.email_access.plugin import EmailAccessPlugin
from assistant_agent.tools.plugins.builtin.image_generation.plugin import ImageGenerationToolPlugin
from assistant_agent.tools.plugins.builtin.local_file_access.plugin import (
    LocalFileAccessPlugin,
)
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.plugin import (
    PersonalAssistantMCPToolPlugin,
)
from assistant_agent.tools.plugins.builtin.python_execution.plugin import (
    PythonExecutionPlugin,
)
from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
from assistant_agent.tools.plugins.builtin.vision_understanding.plugin import (
    VisionUnderstandingPlugin,
)
from assistant_agent.tools.plugins.builtin.visual_image_search.plugin import (
    VisualImageSearchPlugin,
)
from assistant_agent.tools.plugins.builtin.web_access.plugin import WebAccessToolPlugin


def default_tool_plugins() -> tuple[ToolPlugin, ...]:
    """Return the explicit, ordered set of trusted built-in capability plugins."""

    return (
        EmailAccessPlugin(),
        LocalFileAccessPlugin(),
        PythonExecutionPlugin(),
        VisionUnderstandingPlugin(),
        VisualImageSearchPlugin(),
        ShoppingToolPlugin(),
        PersonalAssistantMCPToolPlugin(),
        WebAccessToolPlugin(),
        ImageGenerationToolPlugin(),
        DurableTaskToolPlugin(),
    )
