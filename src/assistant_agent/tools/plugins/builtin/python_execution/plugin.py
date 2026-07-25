"""Governed local Python execution plugin."""

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.python_execution.tool import (
    PythonInterpreterTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class PythonExecutionPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="python_execution", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [PythonInterpreterTool()]
