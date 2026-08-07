"""Explicit, secret-safe configuration for Assistant Memory Plugins."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr


MEMORY_PLUGIN_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH"
MEMORY_PLUGIN_EXPORT = "__assistant_memory_plugin_factory__"
_SECRET_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class MemoryPluginConfigError(ValueError):
    """A safe configuration error that never includes config values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MemoryPluginEntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    module: str
    config: dict[str, JsonValue | SecretStr] = Field(default_factory=dict)


class MemoryPluginsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["assistant_memory_plugins_v1"]
    slot: str
    plugins: dict[str, MemoryPluginEntryConfig]


def load_memory_plugins_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> MemoryPluginsConfig:
    """Load one explicit config file and resolve only declared secret references."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryPluginConfigError("memory_plugin_config_invalid") from exc
    if not isinstance(raw, dict):
        raise MemoryPluginConfigError("memory_plugin_config_invalid")

    source = os.environ if env is None else env
    resolved = _resolve_secret_references(raw, source)
    try:
        return MemoryPluginsConfig.model_validate(resolved)
    except Exception as exc:
        raise MemoryPluginConfigError("memory_plugin_config_invalid") from exc


def _resolve_secret_references(
    value: object,
    env: Mapping[str, str],
) -> object:
    if isinstance(value, dict):
        return {key: _resolve_secret_references(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_secret_references(item, env) for item in value]
    if not isinstance(value, str):
        return value

    match = _SECRET_REFERENCE.fullmatch(value)
    if match is None:
        if "${" in value:
            raise MemoryPluginConfigError("memory_plugin_secret_reference_invalid")
        return value
    secret = env.get(match.group(1))
    if secret is None:
        raise MemoryPluginConfigError("memory_plugin_secret_missing")
    return SecretStr(secret)
