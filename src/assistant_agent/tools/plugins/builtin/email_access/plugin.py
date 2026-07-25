"""Built-in read-only email access Plugin."""

from assistant_agent.tools.plugins.builtin.email_access.backend import (
    EMAIL_READ_TOOL_NAME,
    EMAIL_SEARCH_TOOL_NAME,
    MockEmailBackend,
    WorkspaceMCPEmailBackend,
    configured_email_bindings,
    create_mcp_runner,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    EmailReadTool,
    EmailSearchTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)
from assistant_agent.tools.base import Tool


class EmailAccessPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="email_access",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if context.mock_mode:
            backend = MockEmailBackend()
            return [
                EmailSearchTool(backend),
                EmailReadTool(backend),
            ]

        bindings = configured_email_bindings(context.mcp_server_configs)
        if not bindings:
            return []
        runner = context.mcp_runner or create_mcp_runner(
            context.mcp_server_configs
        )
        if runner is None:
            return []
        backend = WorkspaceMCPEmailBackend(
            runner=runner,
            search_binding=bindings.get(EMAIL_SEARCH_TOOL_NAME),
            read_binding=bindings.get(EMAIL_READ_TOOL_NAME),
        )
        tools: list[Tool] = []
        if EMAIL_SEARCH_TOOL_NAME in bindings:
            tools.append(EmailSearchTool(backend))
        if EMAIL_READ_TOOL_NAME in bindings:
            tools.append(EmailReadTool(backend))
        return tools
