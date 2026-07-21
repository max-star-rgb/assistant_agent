"""Durable task planning tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.task_plan_tool import TaskPlanSubmitTool


class DurableTaskToolPlugin:
    plugin_id = "durable_task"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.config.durable_tasks_enabled or context.durable_task_service is None:
            return []
        return [TaskPlanSubmitTool(context.durable_task_service)]
