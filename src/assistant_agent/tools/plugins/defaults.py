"""Explicit composition root for trusted built-in tool plugins."""

from assistant_agent.tools.plugins.contracts import ToolPlugin
from assistant_agent.tools.plugins.builtin.workflow.plugin import WorkflowToolPlugin
from assistant_agent.tools.plugins.builtin.email_access.plugin import EmailAccessPlugin
from assistant_agent.tools.plugins.builtin.image_generation.plugin import ImageGenerationToolPlugin
from assistant_agent.tools.plugins.builtin.image_to_3d.plugin import ImageTo3DToolPlugin
from assistant_agent.tools.plugins.builtin.local_file_access.plugin import (
    LocalFileAccessPlugin,
)
from assistant_agent.tools.plugins.builtin.skill_loading.plugin import (
    SkillLoadingPlugin,
)
from assistant_agent.tools.plugins.builtin.lodging.plugin import LodgingToolPlugin
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.plugin import (
    CalendarContactsPlugin,
)
from assistant_agent.tools.plugins.builtin.python_execution.plugin import (
    PythonExecutionPlugin,
)
from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
from assistant_agent.tools.plugins.builtin.media_inspection.plugin import (
    MediaInspectionPlugin,
)
from assistant_agent.tools.plugins.builtin.visual_image_search.plugin import (
    VisualImageSearchPlugin,
)
from assistant_agent.tools.plugins.builtin.website_guidance.plugin import (
    WebsiteGuidancePlugin,
)


def default_tool_plugins() -> tuple[ToolPlugin, ...]:
    """Return the explicit, ordered set of trusted built-in capability plugins."""

    return (
        EmailAccessPlugin(),
        LocalFileAccessPlugin(),
        SkillLoadingPlugin(),
        LodgingToolPlugin(),
        PythonExecutionPlugin(),
        MediaInspectionPlugin(),
        VisualImageSearchPlugin(),
        WebsiteGuidancePlugin(),
        ShoppingToolPlugin(),
        CalendarContactsPlugin(),
        ImageGenerationToolPlugin(),
        ImageTo3DToolPlugin(),
        WorkflowToolPlugin(),
    )
