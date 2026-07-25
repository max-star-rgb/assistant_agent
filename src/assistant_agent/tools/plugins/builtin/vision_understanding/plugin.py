"""Image and video understanding plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.services.vision_client import (
    create_realtime_vision_understanding_client,
    create_vision_understanding_client,
)
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.vision_understanding.tool import (
    VisionUnderstandingTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class VisionUnderstandingPlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="vision_understanding",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not vision_provider_ready(context.config):
            return []
        return [
            VisionUnderstandingTool(
                client=create_vision_understanding_client(context.config),
                context_store=context.video_context_store,
                memory_store=context.realtime_video_memory_store,
            )
        ]


def build_realtime_video_observation_tool(
    config: ProviderConfig,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> Tool:
    """Build the specialized governed vision tool used by the realtime observer."""

    if config.provider_mode == "real" and not vision_provider_ready(config):
        raise ValueError("real provider mode requires a configured vision provider")
    return VisionUnderstandingTool(
        client=create_realtime_vision_understanding_client(config),
        memory_store=realtime_video_memory_store,
    )


def vision_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    )
