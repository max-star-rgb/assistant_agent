"""Configuration for inbound MCP tool adapters and external MCP servers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.services.tool_manifest import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
)


MCP_ENABLED_ENV = "MULTIMODAL_AGENT_MCP_ENABLED"
MCP_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MCP_CONFIG_PATH"
DEFAULT_MCP_CONFIG_PATH = ".local/mcp_servers.json"
MCPTransport = Literal["stdio"]
MCPServerPreset = Literal["google_workspace", "todoist", "notion", "slack"]


class MCPPersonalAssistantToolMapping(BaseModel):
    """Map stable personal assistant tools to provider-specific MCP tools."""

    weather_lookup: str | None = None
    calendar_search: str | None = None
    calendar_create: str | None = None
    contacts_search: str | None = None

    def mapped_tool_names(self) -> list[str]:
        """Return all remote MCP tool names referenced by this mapping."""

        return _dedupe(
            [
                item
                for item in (
                    self.weather_lookup,
                    self.calendar_search,
                    self.calendar_create,
                    self.contacts_search,
                )
                if item
            ]
        )

    def read_only_tool_names(self) -> list[str]:
        """Return mapped tools used behind stable read-only assistant tools."""

        return _dedupe(
            [
                item
                for item in (
                    self.weather_lookup,
                    self.calendar_search,
                    self.contacts_search,
                )
                if item
            ]
        )


class MCPToolAdapterConfig(BaseModel):
    """Conservative allowlist for one inbound MCP tool source."""

    server_name: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_tools: list[str] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = "mcp"

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in set(self.allowed_tools)

    def is_read_only(self, tool_name: str) -> bool:
        return tool_name in set(self.read_only_tools)

    def is_enabled_by_default(self, tool_name: str) -> bool:
        return tool_name in set(self.enabled_tools)


class MCPServerConfig(BaseModel):
    """Explicit configuration for one external MCP server."""

    server_name: str = Field(min_length=1)
    preset: MCPServerPreset | None = None
    transport: MCPTransport = "stdio"
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_tools: list[str] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=list)
    personal_assistant_tools: MCPPersonalAssistantToolMapping = Field(
        default_factory=MCPPersonalAssistantToolMapping
    )
    namespace_prefix: str = "mcp"
    timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def apply_preset_defaults(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        preset = data.get("preset")
        preset_defaults = _MCP_SERVER_PRESETS.get(str(preset)) if preset else None
        if not preset_defaults:
            return data
        merged = dict(data)
        for key, value in preset_defaults.items():
            if key == "personal_assistant_tools":
                mapping = dict(value)
                explicit = merged.get(key)
                if isinstance(explicit, dict):
                    mapping.update(explicit)
                elif isinstance(explicit, MCPPersonalAssistantToolMapping):
                    mapping.update(explicit.model_dump(exclude_none=True))
                merged[key] = mapping
                continue
            if not merged.get(key):
                merged[key] = list(value) if isinstance(value, list) else value
        return merged

    @model_validator(mode="after")
    def validate_tool_sets(self) -> "MCPServerConfig":
        self.allowed_tools = _dedupe(self.allowed_tools)
        self.read_only_tools = _dedupe(self.read_only_tools)
        self.enabled_tools = _dedupe(self.enabled_tools)
        allowed = set(self.allowed_tools)
        for field_name, values in (
            ("read_only_tools", self.read_only_tools),
            ("enabled_tools", self.enabled_tools),
        ):
            unknown = sorted(set(values) - allowed)
            if unknown:
                raise ValueError(f"{field_name} contains tools outside allowed_tools: {unknown}")
        mapped_tools = set(self.personal_assistant_tools.mapped_tool_names())
        mapped_unknown = sorted(mapped_tools - allowed)
        if mapped_unknown:
            raise ValueError(
                f"personal_assistant_tools contains tools outside allowed_tools: {mapped_unknown}"
            )
        read_only = set(self.read_only_tools)
        mapped_read_tools = set(self.personal_assistant_tools.read_only_tool_names())
        mapped_not_read_only = sorted(mapped_read_tools - read_only)
        if mapped_not_read_only:
            raise ValueError(
                "personal_assistant_tools read mappings must also be in read_only_tools: "
                f"{mapped_not_read_only}"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP server requires command.")
        return self

    def adapter_config(self) -> MCPToolAdapterConfig:
        """Return the per-server adapter config used for ToolSpec normalization."""

        return MCPToolAdapterConfig(
            server_name=self.server_name,
            allowed_tools=list(self.allowed_tools),
            read_only_tools=list(self.read_only_tools),
            enabled_tools=list(self.enabled_tools),
            namespace_prefix=self.namespace_prefix,
        )


def load_mcp_server_configs_from_env(
    env: Mapping[str, str] | None = None,
    *,
    config_path: str | Path | None = None,
) -> list[MCPServerConfig]:
    """Load external MCP server configs only when explicitly enabled."""

    source = os.environ if env is None else env
    if not _bool_env(source.get(MCP_ENABLED_ENV), False):
        return []
    path = Path(config_path or source.get(MCP_CONFIG_PATH_ENV) or DEFAULT_MCP_CONFIG_PATH).expanduser()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(servers, list):
        return []
    configs: list[MCPServerConfig] = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        try:
            configs.append(MCPServerConfig.model_validate(item))
        except ValueError:
            continue
    return configs


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in deduped:
            deduped.append(value)
    return deduped


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


_MCP_SERVER_PRESETS: dict[str, dict[str, object]] = {
    "google_workspace": {
        "allowed_tools": [
            "search_events",
            "create_event",
            "search_contacts",
            "search_files",
        ],
        "read_only_tools": ["search_events", "search_contacts", "search_files"],
        "enabled_tools": ["search_events", "search_contacts", "search_files"],
        "personal_assistant_tools": {
            CALENDAR_SEARCH_TOOL_NAME: "search_events",
            CALENDAR_CREATE_TOOL_NAME: "create_event",
            CONTACTS_SEARCH_TOOL_NAME: "search_contacts",
        },
    },
    "todoist": {
        "allowed_tools": ["search_tasks", "create_task"],
        "read_only_tools": ["search_tasks"],
        "enabled_tools": ["search_tasks"],
    },
    "notion": {
        "allowed_tools": ["search_pages", "fetch_page", "create_page"],
        "read_only_tools": ["search_pages", "fetch_page"],
        "enabled_tools": ["search_pages", "fetch_page"],
    },
    "slack": {
        "allowed_tools": ["search_messages", "list_channels", "post_message"],
        "read_only_tools": ["search_messages", "list_channels"],
        "enabled_tools": ["search_messages", "list_channels"],
    },
}
