"""Trusted configuration for official external MCP Tool adapters."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


MCP_ENABLED_ENV = "MULTIMODAL_AGENT_MCP_ENABLED"
MCP_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MCP_CONFIG_PATH"
DEFAULT_MCP_CONFIG_PATH = ".local/mcp_servers.json"
MCPTransport = Literal["stdio"]
MCPServerPreset = Literal["google_workspace", "todoist", "notion", "slack"]
_PARENT_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class MCPConfigurationError(ValueError):
    """Enabled MCP configuration does not satisfy the current schema."""


class MCPServerConfig(BaseModel):
    """Explicit configuration for one external MCP server."""

    model_config = ConfigDict(extra="forbid")

    server_name: str = Field(min_length=1)
    preset: MCPServerPreset | None = None
    transport: MCPTransport = "stdio"
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = "mcp"

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
                f"read_only_tools contains tools outside allowed_tools: {unknown}"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP server requires command.")
        return self


def load_mcp_server_configs_from_env(
    env: Mapping[str, str] | None = None,
    *,
    config_path: str | Path | None = None,
) -> list[MCPServerConfig]:
    """Load external MCP server configs only when explicitly enabled."""

    source = os.environ if env is None else env
    if not _bool_env(source.get(MCP_ENABLED_ENV), False):
        return []
    path = Path(
        config_path or source.get(MCP_CONFIG_PATH_ENV) or DEFAULT_MCP_CONFIG_PATH
    ).expanduser()
    if not path.exists():
        raise MCPConfigurationError("enabled MCP configuration file is missing")
    try:
        document = path.read_text(encoding="utf-8")
    except OSError:
        raise MCPConfigurationError(
            "enabled MCP configuration file could not be read"
        ) from None
    try:
        payload = json.loads(document)
    except json.JSONDecodeError:
        raise MCPConfigurationError(
            "enabled MCP configuration file is not valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise MCPConfigurationError("MCP configuration root must be an object")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise MCPConfigurationError("MCP configuration servers must be a list")
    configs: list[MCPServerConfig] = []
    for index, item in enumerate(servers):
        if not isinstance(item, dict):
            raise MCPConfigurationError(
                f"invalid MCP configuration server[{index}]: expected an object"
            )
        try:
            configs.append(MCPServerConfig.model_validate(item))
        except ValidationError as exc:
            fields = sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors()
                }
            )
            detail = ", ".join(fields) or "server"
            raise MCPConfigurationError(
                f"invalid MCP configuration server[{index}] fields: {detail}"
            ) from None
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
