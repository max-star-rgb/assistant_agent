"""Built-in tool capability plugins used by the default registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.services.image_generation_adapter import create_image_generation_adapter
from assistant_agent.services.memory_media_ingestion import create_memory_media_ingestion_service
from assistant_agent.services.personal_assistant_mcp_adapters import (
    configured_personal_assistant_tools,
    create_personal_assistant_adapter_bundle,
)
from assistant_agent.services.product_adapter import (
    create_shopping_compare_adapter,
    create_shopping_search_adapter,
)
from assistant_agent.services.tool_manifest import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
)
from assistant_agent.services.tool_visual_image_search_adapter import (
    create_visual_image_search_adapter,
)
from assistant_agent.services.vision_client import create_vision_understanding_client
from assistant_agent.services.web_fetch_adapter import create_web_fetch_adapter
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.tools.agent_delegation_tool import AgentDelegationTool
from assistant_agent.tools.base import Tool
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.memory_media_tool import MemoryIngestStatusTool, MemoryMediaIngestTool
from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool
from assistant_agent.tools.personal_assistant_tools import (
    CalendarCreateTool,
    CalendarSearchTool,
    ContactsSearchTool,
    WeatherTool,
)
from assistant_agent.tools.python_interpreter_tool import PythonInterpreterTool
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool
from assistant_agent.tools.task_plan_tool import TaskPlanSubmitTool
from assistant_agent.tools.tool_search_tool import ToolSearchTool
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.visual_image_search_tool import VisualImageSearchTool
from assistant_agent.tools.web_fetch_tool import WebFetchTool
from assistant_agent.tools.web_search_tool import WebSearchTool

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
    from assistant_agent.services.agent_communication import AgentCommunicationService
    from assistant_agent.services.durable_tasks.service import DurableTaskService
    from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
    from assistant_agent.services.video_context import VideoContextStore


@dataclass(frozen=True)
class ToolPluginContext:
    """Dependencies and structured enablement facts available to built-in plugins."""

    config: ProviderConfig
    mcp_server_configs: list[MCPServerConfig]
    mcp_runner: MCPToolDiscoveryRunner | None = None
    video_context_store: VideoContextStore | None = None
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None
    enable_agent_delegation: bool = False
    agent_communication_service: AgentCommunicationService | None = None
    durable_task_service: DurableTaskService | None = None

    @property
    def mock_mode(self) -> bool:
        return self.config.provider_mode == "mock"


class ToolPlugin(Protocol):
    """A trusted in-process capability bundle that constructs governed tools."""

    plugin_id: str

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        """Create the tools contributed by this capability bundle."""


class CoreToolPlugin:
    plugin_id = "core"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        return [
            ToolSearchTool(
                server_configs=context.mcp_server_configs,
                runner=context.mcp_runner,
            ),
            PythonInterpreterTool(),
        ]


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


class VisionToolPlugin:
    plugin_id = "vision"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not real_provider_ready(context.config, "vision"):
            return []
        return [
            VisionUnderstandingTool(
                client=create_vision_understanding_client(context.config),
                context_store=context.video_context_store,
                memory_store=context.realtime_video_memory_store,
            )
        ]


class ShoppingToolPlugin:
    plugin_id = "shopping"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not real_provider_ready(context.config, "shopping"):
            return []
        return [
            ShoppingSearchTool(
                search_adapter=create_shopping_search_adapter(context.config),
                compare_adapter=create_shopping_compare_adapter(context.config),
            )
        ]


class PersonalAssistantToolPlugin:
    plugin_id = "personal_assistant"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        tool_names = configured_personal_assistant_tools(context.mcp_server_configs)
        if not context.mock_mode and not tool_names:
            return []
        adapters = create_personal_assistant_adapter_bundle(
            context.config,
            mcp_server_configs=context.mcp_server_configs,
            mcp_runner=context.mcp_runner,
        )
        tools: list[Tool] = []
        if context.mock_mode or WEATHER_TOOL_NAME in tool_names:
            tools.append(WeatherTool(adapter=adapters.weather))
        if context.mock_mode or CALENDAR_SEARCH_TOOL_NAME in tool_names:
            tools.append(CalendarSearchTool(adapter=adapters.calendar))
        if context.mock_mode or CALENDAR_CREATE_TOOL_NAME in tool_names:
            tools.append(CalendarCreateTool(adapter=adapters.calendar))
        if context.mock_mode or CONTACTS_SEARCH_TOOL_NAME in tool_names:
            tools.append(ContactsSearchTool(adapter=adapters.contacts))
        return tools


class WebToolPlugin:
    plugin_id = "web"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not real_provider_ready(context.config, "web"):
            return []
        return [
            WebSearchTool(adapter=create_web_search_adapter(context.config)),
            WebFetchTool(adapter=create_web_fetch_adapter(context.config)),
        ]


class VisualSearchToolPlugin:
    plugin_id = "visual_search"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not real_provider_ready(context.config, "visual_search"):
            return []
        return [
            VisualImageSearchTool(adapter=create_visual_image_search_adapter(context.config))
        ]


class ImageGenerationToolPlugin:
    plugin_id = "image_generation"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not real_provider_ready(context.config, "image_generation"):
            return []
        return [ImageGenerationTool(adapter=create_image_generation_adapter(context.config))]


class AgentDelegationToolPlugin:
    plugin_id = "agent_delegation"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.enable_agent_delegation or context.mock_mode:
            return []
        if context.agent_communication_service is None:
            raise ValueError(
                "agent_communication_service is required when agent delegation is enabled"
            )
        return [AgentDelegationTool(context.agent_communication_service)]


class DurableTaskToolPlugin:
    plugin_id = "durable_task"

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.config.durable_tasks_enabled or context.durable_task_service is None:
            return []
        return [TaskPlanSubmitTool(context.durable_task_service)]


def default_tool_plugins() -> tuple[ToolPlugin, ...]:
    """Return the explicit, ordered set of trusted built-in capability plugins."""

    return (
        CoreToolPlugin(),
        MemoryToolPlugin(),
        VisionToolPlugin(),
        ShoppingToolPlugin(),
        PersonalAssistantToolPlugin(),
        WebToolPlugin(),
        VisualSearchToolPlugin(),
        ImageGenerationToolPlugin(),
        AgentDelegationToolPlugin(),
        DurableTaskToolPlugin(),
    )


def build_default_tools(context: ToolPluginContext) -> list[Tool]:
    """Build default tools without allowing plugins to bypass registry governance."""

    tools: list[Tool] = []
    for plugin in default_tool_plugins():
        tools.extend(plugin.build_tools(context))
    return tools


def real_provider_ready(config: ProviderConfig, capability: str) -> bool:
    """Return whether a provider-backed capability is fully configured."""

    if capability == "vision":
        return (
            config.vision_provider != "mock"
            and not config.resolved_vision_provider().missing_required_env()
        )
    if capability == "image_generation":
        return (
            config.image_generation_provider != "mock"
            and not config.resolved_image_generation_provider().missing_required_env()
        )
    if capability == "web":
        return bool(
            config.search_provider == "http"
            and config.web_search_base_url
            and config.web_search_api_key
        )
    if capability == "visual_search":
        return bool(
            config.visual_image_search_provider == "qwen"
            and config.qwen_image_search_api_key
        )
    if capability == "shopping":
        if config.shopping_search_provider == config.shopping_compare_provider == "haodanku":
            return bool(config.haodanku_api_key)
        if config.shopping_search_provider == config.shopping_compare_provider == "http":
            return bool(
                config.shopping_search_base_url
                and config.shopping_search_api_key
                and config.shopping_compare_base_url
                and config.shopping_compare_api_key
            )
    return False
