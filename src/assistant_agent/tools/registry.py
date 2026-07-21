"""Tool registry and default tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Dict

from pydantic import BaseModel

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.schemas.tool_ids import (
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_INGEST_STATUS_TOOL_NAME,
    MEMORY_MEDIA_INGEST_TOOL_NAME,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_TOOL_NAME,
)
from assistant_agent.services.video_context import VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.defaults import build_default_tools
from assistant_agent.tools.plugins.vision.plugin import (
    build_realtime_video_observation_tool,
)

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
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
        "enabled_by_default",
        "requires_media",
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
    plugin_context = ToolPluginContext(
        config=config,
        mcp_server_configs=resolved_mcp_server_configs or [],
        mcp_runner=mcp_runner,
        video_context_store=video_context_store,
        realtime_video_memory_store=realtime_video_memory_store,
        durable_task_service=durable_task_service,
    )
    for tool in build_default_tools(plugin_context):
        registry.register(tool)
    if not mock_mode and (enable_mcp_tools or mcp_server_configs is not None):
        from assistant_agent.mcp.registration import register_configured_mcp_tools

        server_configs = resolved_mcp_server_configs or []
        register_configured_mcp_tools(registry, server_configs, runner=mcp_runner)
    return registry


def create_realtime_video_observation_registry(
    config: ProviderConfig | None = None,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> ToolRegistry:
    """Create the governed realtime observer registry with one visual tool."""

    config = config or ProviderConfig()
    registry = ToolRegistry()
    registry.register(
        build_realtime_video_observation_tool(
            config,
            realtime_video_memory_store=realtime_video_memory_store,
        )
    )
    return registry
