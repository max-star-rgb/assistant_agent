"""Governed local Python execution plugin."""

from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.python_execution.tool import (
    PythonInterpreterTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class PythonExecutionPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="python_execution", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        return [PythonInterpreterTool()]
