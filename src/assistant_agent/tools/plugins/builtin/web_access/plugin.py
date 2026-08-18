"""Web search and fetch tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.web_access.fetch_backend import (
    create_web_fetch_adapter,
)
from assistant_agent.tools.plugins.builtin.web_access.search_backend import (
    create_web_search_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import WebFetchTool
from assistant_agent.tools.plugins.builtin.web_access.search_tool import WebSearchTool


class WebAccessToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if not context.mock_mode:
            return []
        return [
            WebSearchTool(adapter=create_web_search_adapter(context.config)),
            WebFetchTool(adapter=create_web_fetch_adapter(context.config)),
        ]


def web_provider_ready(config: ProviderConfig) -> bool:
    return config.provider_mode == "mock"
