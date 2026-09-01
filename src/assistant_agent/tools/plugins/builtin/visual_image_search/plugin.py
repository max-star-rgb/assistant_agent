"""Visual image search plugin."""

from assistant_agent.config import SearchConfig
from assistant_agent.tools.plugins.builtin.visual_image_search.backend import (
    create_visual_image_search_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    create_visual_image_search_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class VisualImageSearchPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if not context.mock_mode and not visual_search_provider_ready(context.config.search):
            return []
        return [
            create_visual_image_search_tool(
                adapter=create_visual_image_search_adapter(
                    context.config.search,
                    provider_mode=context.provider_mode,
                )
            )
        ]


def visual_search_provider_ready(config: SearchConfig) -> bool:
    return bool(
        config.visual_image_search_provider == "qwen"
        and config.qwen_image_search_api_key
    )
