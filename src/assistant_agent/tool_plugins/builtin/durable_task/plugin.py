"""Durable task planning tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tool_plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tool_plugins.builtin.durable_task.tool import TaskPlanSubmitTool


class DurableTaskToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="durable_task", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.config.durable_tasks_enabled or context.durable_task_service is None:
            return []
        return [TaskPlanSubmitTool(context.durable_task_service)]
