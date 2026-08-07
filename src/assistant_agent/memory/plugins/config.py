"""Explicit, secret-safe configuration for Assistant Memory Plugins."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeAliasType

from pydantic import BaseModel, ConfigDict, Field, SecretStr


MEMORY_PLUGIN_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH"
MEMORY_PLUGIN_EXPORT = "__assistant_memory_plugin_factory__"
MEMORY_PLUGIN_SLOT_MAX_LENGTH = 128
MEMORY_PLUGIN_REGISTRATION_SOURCE_MAX_LENGTH = 512
MEMORY_PLUGIN_MODULE_SOURCE_PREFIX = "module:"
MEMORY_PLUGIN_MODULE_MAX_LENGTH = (
    MEMORY_PLUGIN_REGISTRATION_SOURCE_MAX_LENGTH
    - len(MEMORY_PLUGIN_MODULE_SOURCE_PREFIX)
)
_SECRET_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

MemoryPluginConfigValue = TypeAliasType(
    "MemoryPluginConfigValue",
    "str | int | float | bool | None | SecretStr | list[MemoryPluginConfigValue] "
    "| dict[str, MemoryPluginConfigValue]",
)


class MemoryPluginConfigError(ValueError):
    """A safe configuration error that never includes config values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKeyError(ValueError):
    """Internal signal for JSON objects that would silently overwrite a key."""


class MemoryPluginEntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    module: str = Field(min_length=1, max_length=MEMORY_PLUGIN_MODULE_MAX_LENGTH)
    config: dict[str, MemoryPluginConfigValue] = Field(default_factory=dict)


class MemoryPluginsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["assistant_memory_plugins_v1"]
    slot: str = Field(min_length=1, max_length=MEMORY_PLUGIN_SLOT_MAX_LENGTH)
    plugins: dict[str, MemoryPluginEntryConfig]


def load_memory_plugins_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> MemoryPluginsConfig:
    """Load one explicit config file and resolve only declared secret references."""

    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except _DuplicateJsonKeyError:
        raise MemoryPluginConfigError("memory_plugin_config_duplicate_key") from None
    except Exception:
        raise MemoryPluginConfigError("memory_plugin_config_invalid") from None
    if not isinstance(raw, dict):
        raise MemoryPluginConfigError("memory_plugin_config_invalid")

    source = os.environ if env is None else env
    try:
        resolved = _resolve_secret_references(raw, source)
    except MemoryPluginConfigError:
        raise
    except Exception:
        raise MemoryPluginConfigError("memory_plugin_config_invalid") from None
    if not _is_valid_memory_plugin_slot(resolved.get("slot")):
        raise MemoryPluginConfigError("memory_plugin_slot_invalid")
    try:
        return MemoryPluginsConfig.model_validate(resolved)
    except Exception:
        raise MemoryPluginConfigError("memory_plugin_config_invalid") from None


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
        return value
    secret = env.get(match.group(1))
    if secret is None:
        raise MemoryPluginConfigError("memory_plugin_secret_missing")
    return SecretStr(secret)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError()
        result[key] = value
    return result


def _is_valid_memory_plugin_slot(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MEMORY_PLUGIN_SLOT_MAX_LENGTH
    )
