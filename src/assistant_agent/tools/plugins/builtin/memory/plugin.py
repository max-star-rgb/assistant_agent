"""Expose explicit memory writes only when a Mem0 backend is configured."""

from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.memory.tool import create_memory_add_tool


class MemoryToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        backend = context.memory_backend
        if context.mock_mode or backend is None or backend.backend_id not in {"mem0", "hybrid"}:
            return []
        return [create_memory_add_tool(backend)]
