"""Built-in read-only email access Plugin."""

from assistant_agent.tools.plugins.builtin.email_access.backend import (
    MockEmailBackend,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    create_email_read_tool,
    create_email_search_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from langchain_core.tools import BaseTool


class EmailAccessPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if not context.mock_mode:
            return []
        backend = MockEmailBackend()
        return [create_email_search_tool(backend), create_email_read_tool(backend)]
