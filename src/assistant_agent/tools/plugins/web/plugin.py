"""Web search and fetch tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.services.web_fetch_adapter import create_web_fetch_adapter
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tools.plugins.web.fetch_tool import WebFetchTool
from assistant_agent.tools.plugins.web.search_tool import WebSearchTool


class WebToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="web", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not web_provider_ready(context.config):
            return []
        return [
            WebSearchTool(adapter=create_web_search_adapter(context.config)),
            WebFetchTool(adapter=create_web_fetch_adapter(context.config)),
        ]


def web_provider_ready(config: ProviderConfig) -> bool:
    return bool(
        config.search_provider == "http"
        and config.web_search_base_url
        and config.web_search_api_key
    )
