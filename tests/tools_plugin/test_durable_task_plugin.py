"""Real-mode construction test for the durable-task tool plugin."""

from dataclasses import replace

from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.durable_task.plugin import DurableTaskToolPlugin
from assistant_agent.tools.registry import ToolRegistry


def test_durable_task_plugin_builds_tool_with_real_mode_context(
    real_plugin_context: ToolPluginContext,
) -> None:
    service = DurableTaskService(store=InMemoryTaskStore(), registry=ToolRegistry())
    context = ToolPluginContext(
        config=replace(real_plugin_context.config, durable_tasks_enabled=True),
        mcp_server_configs=real_plugin_context.mcp_server_configs,
        durable_task_service=service,
    )

    tools = DurableTaskToolPlugin().build_tools(context)

    assert context.mock_mode is False
    assert [tool.name for tool in tools] == ["task_plan_submit"]
