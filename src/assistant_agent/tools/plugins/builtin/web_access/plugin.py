"""Web fetch tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.web_access.fetch_backend import (
    create_web_fetch_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import (
    create_web_fetch_tool,
)


class WebAccessToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if not context.mock_mode:
            return []
        return [
            create_web_fetch_tool(adapter=create_web_fetch_adapter(context.config)),
        ]


def web_provider_ready(config: ProviderConfig) -> bool:
    return config.provider_mode == "mock"
