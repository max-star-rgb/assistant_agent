"""Attached and live media inspection plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.media.vision.vision_client import (
    create_vision_understanding_client,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    MediaInspectTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    VisualReminderManageTool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class MediaInspectionPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        tools: list[BaseTool] = []
        vision_ready = context.mock_mode or vision_provider_ready(context.config)
        vision_client = context.vision_client
        if (
            context.embedding_coordinator_store is not None
            and context.visual_reminder_registry is not None
        ):
            tools.append(
                VisualReminderManageTool(
                    coordinator_store=context.embedding_coordinator_store,
                    reminder_registry=context.visual_reminder_registry,
                )
            )
        if vision_ready:
            tools.extend(
                [
                    MediaInspectTool(
                        client=(
                            vision_client
                            or create_vision_understanding_client(context.config)
                        ),
                        context_store=context.video_context_store,
                        memory_store=context.realtime_video_memory_store,
                    ),
                    LiveViewInspectTool(
                        client=(
                            vision_client
                            or create_vision_understanding_client(context.config)
                        ),
                        context_store=context.video_context_store,
                        memory_store=context.realtime_video_memory_store,
                        semantic_store_pool=context.visual_semantic_store_pool,
                    ),
                ]
            )
        if (
            vision_ready
            and context.visual_semantic_store_pool is not None
            and context.visual_memory_text_index is not None
        ):
            tools.append(
                VisualMemorySearchTool(
                    semantic_store_pool=context.visual_semantic_store_pool,
                    text_index=context.visual_memory_text_index,
                    limit=context.config.visual_memory_result_limit,
                )
            )
        return tools


def vision_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    )
