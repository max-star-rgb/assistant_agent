"""Configuration for inbound MCP tool adapters and external MCP servers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.tools.ids import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
)


MCP_ENABLED_ENV = "MULTIMODAL_AGENT_MCP_ENABLED"
MCP_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MCP_CONFIG_PATH"
DEFAULT_MCP_CONFIG_PATH = ".local/mcp_servers.json"
MCPTransport = Literal["stdio"]
MCPServerPreset = Literal["google_workspace", "todoist", "notion", "slack"]
MCPCalendarAdapterProfile = Literal["passthrough", "workspace_mcp_v1"]
MCPEmailAdapterProfile = Literal["passthrough", "workspace_mcp_v1"]
_PARENT_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class MCPPersonalAssistantToolMapping(BaseModel):
    """Map stable calendar and contacts tools to provider-specific MCP tools."""

    weather_lookup: str | None = Field(
        default=None,
        exclude=True,
        description="Deprecated weather mapping retained only for registration suppression.",
    )
    calendar_search: str | None = None
    calendar_create: str | None = None
    contacts_search: str | None = None
    calendar_profile: MCPCalendarAdapterProfile = "passthrough"
    calendar_user_email: str | None = None

    @model_validator(mode="after")
    def validate_adapter_profiles(self) -> "MCPPersonalAssistantToolMapping":
        if self.calendar_profile == "workspace_mcp_v1":
            if self.calendar_search and self.calendar_search != "get_events":
                raise ValueError("workspace_mcp_v1 requires calendar_search=get_events")
            if self.calendar_create and self.calendar_create != "manage_event":
                raise ValueError("workspace_mcp_v1 requires calendar_create=manage_event")
            if (self.calendar_search or self.calendar_create) and not (
                self.calendar_user_email and self.calendar_user_email.strip()
            ):
                raise ValueError("workspace_mcp_v1 requires calendar_user_email")
        return self

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


class MCPEmailToolMapping(BaseModel):
    """Map stable read-only email Tools to provider-specific MCP tools."""

    search: str | None = None
    read_batch: str | None = None
    profile: MCPEmailAdapterProfile = "passthrough"
    user_email: str | None = None

    @model_validator(mode="after")
    def validate_adapter_profile(self) -> "MCPEmailToolMapping":
        if self.profile != "workspace_mcp_v1":
            return self
        if self.search and self.search != "search_gmail_messages":
            raise ValueError(
                "workspace_mcp_v1 requires email search=search_gmail_messages"
            )
        if (
            self.read_batch
            and self.read_batch != "get_gmail_messages_content_batch"
        ):
            raise ValueError(
                "workspace_mcp_v1 requires "
                "email read_batch=get_gmail_messages_content_batch"
            )
        if (self.search or self.read_batch) and not (
            self.user_email and self.user_email.strip()
        ):
            raise ValueError("workspace_mcp_v1 email tools require user_email")
        return self

    def mapped_tool_names(self) -> list[str]:
        return _dedupe(
            [
                tool_name
                for tool_name in (self.search, self.read_batch)
                if tool_name
            ]
        )

    def read_only_tool_names(self) -> list[str]:
        return self.mapped_tool_names()


class MCPToolAdapterConfig(BaseModel):
    """Conservative allowlist for one inbound MCP tool source."""

    server_name: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = "mcp"

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in set(self.allowed_tools)

    def is_read_only(self, tool_name: str) -> bool:
        return tool_name in set(self.read_only_tools)

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
    personal_assistant_tools: MCPPersonalAssistantToolMapping = Field(
        default_factory=MCPPersonalAssistantToolMapping
    )
    email_tools: MCPEmailToolMapping = Field(
        default_factory=MCPEmailToolMapping
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
            if key in {"personal_assistant_tools", "email_tools"}:
                mapping = dict(value)
                explicit = merged.get(key)
                if isinstance(explicit, dict):
                    mapping.update(explicit)
                elif isinstance(
                    explicit,
                    (MCPPersonalAssistantToolMapping, MCPEmailToolMapping),
                ):
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
        allowed = set(self.allowed_tools)
        unknown = sorted(set(self.read_only_tools) - allowed)
        if unknown:
            raise ValueError(
                "read_only_tools contains tools outside allowed_tools: "
                f"{unknown}"
            )
        mapped_tools = set(self.personal_assistant_tools.mapped_tool_names())
        email_mapped_tools = set(self.email_tools.mapped_tool_names())
        for mapping_name, mapped in (
            ("personal_assistant_tools", mapped_tools),
            ("email_tools", email_mapped_tools),
        ):
            mapped_unknown = sorted(mapped - allowed)
            if mapped_unknown:
                raise ValueError(
                    f"{mapping_name} contains tools outside allowed_tools: "
                    f"{mapped_unknown}"
                )
        read_only = set(self.read_only_tools)
        mapped_read_tools = set(self.personal_assistant_tools.read_only_tool_names())
        email_read_tools = set(self.email_tools.read_only_tool_names())
        for mapping_name, mapped_read in (
            ("personal_assistant_tools", mapped_read_tools),
            ("email_tools", email_read_tools),
        ):
            mapped_not_read_only = sorted(mapped_read - read_only)
            if mapped_not_read_only:
                raise ValueError(
                    f"{mapping_name} read mappings must also be in "
                    f"read_only_tools: {mapped_not_read_only}"
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


def resolve_mcp_server_env(
    server_env: Mapping[str, str],
    *,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve only explicit whole-value parent environment references."""

    parent = os.environ if parent_env is None else parent_env
    resolved: dict[str, str] = {}
    for key, value in server_env.items():
        normalized_key = str(key)
        normalized_value = str(value)
        reference = _PARENT_ENV_REFERENCE.fullmatch(normalized_value)
        if reference is None:
            resolved[normalized_key] = normalized_value
            continue
        inherited = parent.get(reference.group(1))
        if inherited is not None:
            resolved[normalized_key] = inherited
    return resolved


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
        "personal_assistant_tools": {
            CALENDAR_SEARCH_TOOL_NAME: "search_events",
            CALENDAR_CREATE_TOOL_NAME: "create_event",
            CONTACTS_SEARCH_TOOL_NAME: "search_contacts",
        },
    },
    "todoist": {
        "allowed_tools": ["search_tasks", "create_task"],
        "read_only_tools": ["search_tasks"],
    },
    "notion": {
        "allowed_tools": ["search_pages", "fetch_page", "create_page"],
        "read_only_tools": ["search_pages", "fetch_page"],
    },
    "slack": {
        "allowed_tools": ["search_messages", "list_channels", "post_message"],
        "read_only_tools": ["search_messages", "list_channels"],
    },
}
