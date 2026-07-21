"""Memory tool plugin."""

from assistant_agent.services.memory_media_ingestion import create_memory_media_ingestion_service
from assistant_agent.tools.base import Tool
from assistant_agent.tools.memory_media_tool import MemoryIngestStatusTool, MemoryMediaIngestTool
from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class MemoryToolPlugin:
    plugin_id = "memory"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        media_service = create_memory_media_ingestion_service(context.config)
        return [
            MemoryRetrievalTool(),
            MemorySaveTool(),
            MemoryMediaIngestTool(media_service),
            MemoryIngestStatusTool(media_service),
        ]
