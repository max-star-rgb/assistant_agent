"""Normalize inbound MCP tool definitions into governed internal tools."""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field, create_model

from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.tools.base import ToolContext


class MCPToolDefinition(BaseModel):
    """External MCP tool definition before internal normalization."""

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolRunner(Protocol):
    """Execution boundary for an already allowlisted remote MCP tool."""

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        """Run a remote MCP tool and return a structured result."""


class MCPProxyTool:
    """ToolRegistry-compatible proxy for one allowlisted MCP tool."""

    def __init__(
        self,
        *,
        config: MCPToolAdapterConfig,
        definition: MCPToolDefinition,
        runner: MCPToolRunner,
    ) -> None:
        self._config = config
        self._definition = definition
        self._runner = runner
        self.name = _namespaced_tool_name(config, definition.name)
        self.description = definition.description
        self.input_schema = _input_model_for_definition(definition)
        self.output_schema = self.input_schema
        self.category = "read" if config.is_read_only(definition.name) else "write"
        self.requires_confirmation = not config.is_read_only(definition.name)
        self.toolset = f"mcp.{_safe_name(config.server_name)}"
        self.enabled_by_default = config.is_enabled_by_default(definition.name)

    def run(
        self,
        input: BaseModel | dict[str, Any],
        context: ToolContext | None = None,
    ) -> ToolResult:
        try:
            payload = (
                input
                if isinstance(input, self.input_schema)
                else self.input_schema.model_validate(input)
            )
            return self._runner.run_tool(
                server_name=self._config.server_name,
                tool_name=self._definition.name,
                tool_input=payload.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - defensive proxy boundary
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=sanitize_error_message(exc),
            )


class MCPToolAdapter:
    """Normalize allowlisted MCP tools into ToolSpec or proxy tool objects."""

    def __init__(
        self,
        config: MCPToolAdapterConfig,
        *,
        runner: MCPToolRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner

    def tool_spec_for_definition(self, definition: MCPToolDefinition) -> ToolSpec | None:
        if not self.config.is_allowed(definition.name):
            return None
        return ToolSpec(
            name=_namespaced_tool_name(self.config, definition.name),
            description=definition.description,
            input_schema=_canonical_input_schema(definition.input_schema),
            category="read" if self.config.is_read_only(definition.name) else "write",
            requires_confirmation=not self.config.is_read_only(definition.name),
            toolset=f"mcp.{_safe_name(self.config.server_name)}",
            enabled_by_default=self.config.is_enabled_by_default(definition.name),
        )

    def proxy_tool_for_definition(self, definition: MCPToolDefinition) -> MCPProxyTool:
        if not self.config.is_allowed(definition.name):
            raise ValueError(f"MCP tool is not allowlisted: {definition.name}")
        if self.runner is None:
            raise ValueError("runner is required to create an MCP proxy tool")
        return MCPProxyTool(config=self.config, definition=definition, runner=self.runner)

    def namespaced_tool_name(self, tool_name: str) -> str:
        return namespaced_mcp_tool_name(self.config, tool_name)


def namespaced_mcp_tool_name(config: MCPToolAdapterConfig, tool_name: str) -> str:
    return _namespaced_tool_name(config, tool_name)


def _namespaced_tool_name(config: MCPToolAdapterConfig, tool_name: str) -> str:
    return ".".join(
        (
            _safe_name(config.namespace_prefix),
            _safe_name(config.server_name),
            _safe_name(tool_name),
        )
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_") or "unknown"


def _input_model_for_definition(definition: MCPToolDefinition) -> type[BaseModel]:
    properties = definition.input_schema.get("properties")
    required = set(_required_inputs(definition.input_schema))
    fields: dict[str, tuple[type[Any], Any]] = {}
    if isinstance(properties, dict):
        for field_name, field_schema in properties.items():
            if not isinstance(field_name, str) or not isinstance(field_schema, dict):
                continue
            field_type = _python_type_for_schema(field_schema)
            default = ... if field_name in required else None
            fields[field_name] = (field_type, default)
    model_name = "".join(part.title() for part in definition.name.split("_")) or "MCPInput"
    return create_model(f"{model_name}Input", **fields)


def _python_type_for_schema(field_schema: dict[str, Any]) -> type[Any]:
    schema_type = field_schema.get("type")
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        return list[Any]
    if schema_type == "object":
        return dict[str, Any]
    return str


def _required_inputs(input_schema: dict[str, Any]) -> list[str]:
    required = input_schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if isinstance(item, str)]


def _canonical_input_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    schema = dict(input_schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["required"] = _required_inputs(schema)
    schema.pop("additionalProperties", None)
    return schema
