"""Explicit Workflow Tool plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.workflow.tool import WorkflowSubmitTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor


class WorkflowToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="durable_workflow", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        service = context.workflow_service
        if (
            not context.config.durable_workflows_enabled
            or service is None
            or not service.definitions.list_types()
        ):
            return []
        return [WorkflowSubmitTool(service)]
