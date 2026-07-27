"""Adapters from ToolSpec to provider/protocol tool schema formats."""

from __future__ import annotations

from typing import Any, Iterable

from assistant_agent.tools.models import ToolSpec


_EXECUTION_ONLY_SCHEMA_KEYS = {
    "additionalProperties",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "pattern",
    "uniqueItems",
}


def tool_spec_to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """Convert ToolSpec to an OpenAI-compatible chat completions tool."""

    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": _native_description(spec),
            "parameters": _provider_schema(spec.input_schema, root=True),
        },
    }


def tool_specs_to_openai_tools(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Convert multiple ToolSpecs to OpenAI-compatible tool definitions."""

    return [tool_spec_to_openai_tool(spec) for spec in specs]


def tool_spec_to_mcp_tool(spec: ToolSpec) -> dict[str, Any]:
    """Convert ToolSpec to a minimal MCP-style tool definition."""

    return {
        "name": spec.name,
        "description": _native_description(spec),
        "inputSchema": _provider_schema(spec.input_schema, root=True),
    }


def tool_specs_to_mcp_tools(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Convert multiple ToolSpecs to MCP-style tool definitions."""

    return [tool_spec_to_mcp_tool(spec) for spec in specs]


def _native_description(spec: ToolSpec) -> str:
    return spec.description.strip()


def _provider_schema(value: Any, *, root: bool = False) -> Any:
    """Normalize Pydantic JSON Schema for Qwen/OpenAI-compatible tools."""

    if isinstance(value, list):
        return [_provider_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if (
            key in {"title", "default"}
            or key in _EXECUTION_ONLY_SCHEMA_KEYS
            or (root and key == "description")
        ):
            continue
        if key == "properties" and isinstance(item, dict):
            normalized[key] = {
                name: _provider_schema(field_schema)
                for name, field_schema in item.items()
            }
            continue
        normalized[key] = _provider_schema(item)
    any_of = normalized.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2:
        concrete = [item for item in any_of if item != {"type": "null"}]
        if len(concrete) == 1 and isinstance(concrete[0], dict):
            normalized = {
                **concrete[0],
                **{key: item for key, item in normalized.items() if key != "anyOf"},
            }
    if normalized.get("required") == []:
        normalized.pop("required", None)
    return normalized
