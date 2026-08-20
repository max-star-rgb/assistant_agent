"""Uploaded, live, and historical visual inspection plugin."""

from assistant_agent.config import ProviderConfig
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.media_inspection.live_tool import (
    create_live_view_inspect_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    create_visual_memory_search_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    create_visual_reminder_manage_tool,
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
                create_visual_reminder_manage_tool(
                    coordinator_store=context.embedding_coordinator_store,
                    reminder_registry=context.visual_reminder_registry,
                )
            )
        if vision_ready and vision_client is not None:
            tools.extend(
                [
                    create_uploaded_media_inspect_tool(
                        vision_client,
                        context_store=context.video_context_store,
                    ),
                    create_live_view_inspect_tool(
                        vision_client,
                        context_store=context.video_context_store,
                        memory_store=context.realtime_video_memory_store,
                        semantic_store_pool=context.visual_semantic_store_pool,
                        live_view_resolver=context.live_view_resolver,
                    ),
                ]
            )
        if (
            context.visual_semantic_store_pool is not None
            and context.visual_memory_text_index is not None
        ):
            tools.append(
                create_visual_memory_search_tool(
                    semantic_store_pool=context.visual_semantic_store_pool,
                    text_index=context.visual_memory_text_index,
                    limit=context.config.visual_memory_result_limit,
                    live_view_resolver=context.live_view_resolver,
                )
            )
        return tools


def vision_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    )
