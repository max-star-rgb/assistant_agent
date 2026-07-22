"""Memory tool plugin."""

from assistant_agent.services.memory_media_ingestion import create_memory_media_ingestion_service
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tools.plugins.memory.media_tools import (
    MemoryIngestStatusTool,
    MemoryMediaIngestTool,
)
from assistant_agent.tools.plugins.memory.tools import MemoryGetTool, MemorySearchTool


class MemoryToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="memory", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        media_service = create_memory_media_ingestion_service(context.config)
        return [
            MemorySearchTool(),
            MemoryGetTool(),
            MemoryMediaIngestTool(media_service),
            MemoryIngestStatusTool(media_service),
        ]
