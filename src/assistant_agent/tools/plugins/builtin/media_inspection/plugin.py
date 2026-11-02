"""Uploaded and live visual inspection plugin."""

from assistant_agent.config import VisionConfig
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.media_inspection.live_tool import (
    create_live_view_inspect_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class MediaInspectionPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        tools: list[BaseTool] = []
        vision_ready = context.mock_mode or vision_provider_ready(context.vision_config)
        vision_client = context.vision_client
        if vision_ready and vision_client is not None:
            tools.extend(
                [
                    create_uploaded_media_inspect_tool(
                        vision_client,
                        max_video_bytes=context.media_config.max_video_bytes,
                        max_video_seconds=context.media_config.max_video_seconds,
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
        return tools


def vision_provider_ready(config: VisionConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_provider().missing_required_env()
    )
