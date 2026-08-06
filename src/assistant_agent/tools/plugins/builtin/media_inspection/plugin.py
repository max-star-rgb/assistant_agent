"""Attached and live media inspection plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.vision.vision_client import (
    create_realtime_vision_understanding_client,
    create_vision_understanding_client,
)
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    MediaInspectTool,
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    VisualReminderManageTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class MediaInspectionPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="media_inspection",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        tools: list[Tool] = []
        vision_ready = context.mock_mode or vision_provider_ready(context.config)
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
                        client=create_vision_understanding_client(context.config),
                        context_store=context.video_context_store,
                        memory_store=context.realtime_video_memory_store,
                    ),
                    LiveViewInspectTool(
                        client=create_vision_understanding_client(context.config),
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


def build_realtime_video_observation_tool(
    config: ProviderConfig,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> Tool:
    """Build the governed media tool used by the realtime observer."""

    if config.provider_mode == "real" and not vision_provider_ready(config):
        raise ValueError("real provider mode requires a configured vision provider")
    return RealtimeVideoObserveTool(
        client=create_realtime_vision_understanding_client(config),
        memory_store=realtime_video_memory_store,
    )


def vision_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    )
