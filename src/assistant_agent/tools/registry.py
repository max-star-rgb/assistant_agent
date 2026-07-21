"""Tool registry and default tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Dict

from pydantic import BaseModel

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.agent_delegation_tool import AgentDelegationTool
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
from assistant_agent.tools.tool_search_tool import ToolSearchTool
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.services.web_fetch_adapter import create_web_fetch_adapter
from assistant_agent.services.image_generation_adapter import create_image_generation_adapter
from assistant_agent.services.memory_media_ingestion import create_memory_media_ingestion_service
from assistant_agent.services.personal_assistant_mcp_adapters import (
    configured_personal_assistant_tools,
    create_personal_assistant_adapter_bundle,
)
from assistant_agent.services.product_adapter import create_shopping_compare_adapter, create_shopping_search_adapter
from assistant_agent.services.tool_manifest import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_INGEST_STATUS_TOOL_NAME,
    MEMORY_MEDIA_INGEST_TOOL_NAME,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_TOOL_NAME,
    WEATHER_TOOL_NAME,
)
from assistant_agent.services.tool_visual_image_search_adapter import create_visual_image_search_adapter
from assistant_agent.services.vision_client import (
    create_realtime_vision_understanding_client,
    create_vision_understanding_client,
)
from assistant_agent.services.video_context import VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.visual_image_search_tool import VisualImageSearchTool
from assistant_agent.tools.web_search_tool import WebSearchTool
from assistant_agent.tools.web_fetch_tool import WebFetchTool
from assistant_agent.tools.task_plan_tool import TaskPlanSubmitTool

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
    from assistant_agent.services.agent_communication import AgentCommunicationService
    from assistant_agent.services.durable_tasks.service import DurableTaskService


_REGISTERED_TOOL_CONTRACTS: dict[str, dict[str, Any]] = {}


class ToolRegistry:
    """In-memory registry for tool lookup and execution."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        spec = self._tool_spec(tool)
        self._tools[tool.name] = tool
        _REGISTERED_TOOL_CONTRACTS[tool.name] = {
            "category": spec.category,
            "requires_confirmation": spec.requires_confirmation,
        }

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool not registered: {name}") from exc

    def list(self) -> list[str]:
        return sorted(self._tools)

    def run(
        self,
        name: str,
        input: BaseModel | dict[str, Any],
        context: ToolContext | None = None,
    ) -> ToolResult:
        return self.get(name).run(input, context)

    def get_spec(self, name: str) -> ToolSpec:
        """Return the provider-neutral contract for one registered tool."""

        return self._tool_spec(self.get(name))

    def list_specs(self) -> list[ToolSpec]:
        """Return provider-neutral specs for all registered tools."""

        return [self._tool_spec(self._tools[name]) for name in sorted(self._tools)]

    @staticmethod
    def _tool_spec(tool: Tool) -> ToolSpec:
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=_schema_to_dict(tool.input_schema, tool_name=tool.name),
            **_declared_contract(tool),
        )

    def describe_tools(self) -> List[Dict[str, Any]]:
        """Return legacy dict descriptions of all registered tools for the assistant."""

        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def _schema_to_dict(schema_type, *, tool_name: str | None = None):
    """Convert a Pydantic model to a safe schema description."""
    try:
        schema = schema_type.model_json_schema()
        definitions = schema.get("$defs", {})
        normalized = _inline_local_schema_refs(schema, definitions)
        normalized.pop("$defs", None)
        properties = normalized.get("properties", {})
        required = list(normalized.get("required", []))
        for field_name in list(properties):
            if _hide_runtime_identity_field(tool_name, field_name):
                properties.pop(field_name, None)
                required = [item for item in required if item != field_name]
        normalized["properties"] = properties
        normalized["required"] = required
        return _close_object_schemas(normalized)
    except Exception:
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }


def _inline_local_schema_refs(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_local_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        target = definitions.get(name, {})
        merged = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
        return _inline_local_schema_refs(merged, definitions)
    return {
        key: _inline_local_schema_refs(item, definitions)
        for key, item in value.items()
        if key != "$defs"
    }


def _close_object_schemas(value: Any) -> Any:
    if isinstance(value, list):
        return [_close_object_schemas(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _close_object_schemas(item) for key, item in value.items()}
    if normalized.get("type") == "object" or "properties" in normalized:
        normalized["additionalProperties"] = False
    return normalized


def _hide_runtime_identity_field(tool_name: str | None, field_name: str) -> bool:
    if tool_name == IMAGE_UNDERSTANDING_TOOL_NAME and field_name in {
        "frame_refs",
        "context_id",
        "metadata",
        "memory_context",
        "sample_strategy",
        "user_id",
        "session_id",
    }:
        return True
    return tool_name in {
        MEMORY_RETRIEVAL_TOOL_NAME,
        MEMORY_SAVE_TOOL_NAME,
        MEMORY_MEDIA_INGEST_TOOL_NAME,
        MEMORY_INGEST_STATUS_TOOL_NAME,
    } and field_name in {"user_id", "session_id"}


def _declared_contract(tool: Tool) -> dict[str, Any]:
    """Read the small set of ToolSpec fields a local or MCP tool may declare."""

    fields = (
        "category",
        "toolset",
        "requires_confirmation",
        "requires_env",
        "enabled_by_default",
        "skill_only",
        "requires_media",
        "progress_message",
    )
    return {name: getattr(tool, name) for name in fields if hasattr(tool, name)}


def tool_contract_fields(tool_name: str) -> dict[str, Any]:
    """Return the last registered contract for legacy realtime projections."""

    return {
        "category": "dangerous",
        "requires_confirmation": True,
        **_REGISTERED_TOOL_CONTRACTS.get(tool_name, {}),
    }



def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
    enable_agent_delegation: bool = False,
    agent_communication_service: AgentCommunicationService | None = None,
    durable_task_service: DurableTaskService | None = None,
    enable_mcp_tools: bool = False,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_config_path: str | None = None,
    mcp_runner: MCPToolDiscoveryRunner | None = None,
) -> ToolRegistry:
    config = config or ProviderConfig()
    registry = ToolRegistry()
    mock_mode = config.provider_mode == "mock"
    resolved_mcp_server_configs = mcp_server_configs
    if (enable_mcp_tools or not mock_mode) and resolved_mcp_server_configs is None:
        from assistant_agent.mcp.config import load_mcp_server_configs_from_env

        resolved_mcp_server_configs = load_mcp_server_configs_from_env(config_path=mcp_config_path)
    if mock_mode:
        resolved_mcp_server_configs = []
    memory_media_service = create_memory_media_ingestion_service(config)
    tools: list[Tool] = [
        ToolSearchTool(
            server_configs=resolved_mcp_server_configs or [],
            runner=mcp_runner,
        ),
        PythonInterpreterTool(),
        MemoryRetrievalTool(),
        MemorySaveTool(),
        MemoryMediaIngestTool(memory_media_service),
        MemoryIngestStatusTool(memory_media_service),
    ]
    if mock_mode or _real_provider_ready(config, "vision"):
        tools.append(
            VisionUnderstandingTool(
                client=create_vision_understanding_client(config),
                context_store=video_context_store,
                memory_store=realtime_video_memory_store,
            )
        )
    if mock_mode or _real_provider_ready(config, "shopping"):
        tools.append(
            ShoppingSearchTool(
                search_adapter=create_shopping_search_adapter(config),
                compare_adapter=create_shopping_compare_adapter(config),
            )
        )
    personal_tool_names = configured_personal_assistant_tools(
        resolved_mcp_server_configs or []
    )
    if mock_mode or personal_tool_names:
        personal_adapters = create_personal_assistant_adapter_bundle(
            config,
            mcp_server_configs=resolved_mcp_server_configs,
            mcp_runner=mcp_runner,
        )
        if mock_mode or WEATHER_TOOL_NAME in personal_tool_names:
            tools.append(WeatherTool(adapter=personal_adapters.weather))
        if mock_mode or CALENDAR_SEARCH_TOOL_NAME in personal_tool_names:
            tools.append(CalendarSearchTool(adapter=personal_adapters.calendar))
        if mock_mode or CALENDAR_CREATE_TOOL_NAME in personal_tool_names:
            tools.append(CalendarCreateTool(adapter=personal_adapters.calendar))
        if mock_mode or CONTACTS_SEARCH_TOOL_NAME in personal_tool_names:
            tools.append(ContactsSearchTool(adapter=personal_adapters.contacts))
    if mock_mode or _real_provider_ready(config, "web"):
        tools.extend(
            (
                WebSearchTool(adapter=create_web_search_adapter(config)),
                WebFetchTool(adapter=create_web_fetch_adapter(config)),
            )
        )
    if mock_mode or _real_provider_ready(config, "visual_search"):
        tools.append(VisualImageSearchTool(adapter=create_visual_image_search_adapter(config)))
    if mock_mode or _real_provider_ready(config, "image_generation"):
        tools.append(ImageGenerationTool(adapter=create_image_generation_adapter(config)))
    for tool in tools:
        registry.register(tool)
    if enable_agent_delegation and not mock_mode:
        if agent_communication_service is None:
            raise ValueError("agent_communication_service is required when agent delegation is enabled")
        registry.register(AgentDelegationTool(agent_communication_service))
    if config.durable_tasks_enabled:
        if durable_task_service is not None:
            registry.register(TaskPlanSubmitTool(durable_task_service))
    if not mock_mode and (enable_mcp_tools or mcp_server_configs is not None):
        from assistant_agent.mcp.registration import register_configured_mcp_tools

        server_configs = resolved_mcp_server_configs or []
        register_configured_mcp_tools(registry, server_configs, runner=mcp_runner)
    return registry


def _real_provider_ready(config: ProviderConfig, capability: str) -> bool:
    if capability == "vision":
        return config.vision_provider != "mock" and not config.resolved_vision_provider().missing_required_env()
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


def create_realtime_video_observation_registry(
    config: ProviderConfig | None = None,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> ToolRegistry:
    """Create the governed realtime observer registry with one visual tool."""

    config = config or ProviderConfig()
    if config.provider_mode == "real" and not _real_provider_ready(config, "vision"):
        raise ValueError("real provider mode requires a configured vision provider")
    registry = ToolRegistry()
    registry.register(
        VisionUnderstandingTool(
            client=create_realtime_vision_understanding_client(config),
            memory_store=realtime_video_memory_store,
        )
    )
    return registry
