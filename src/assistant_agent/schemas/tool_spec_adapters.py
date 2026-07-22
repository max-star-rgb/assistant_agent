"""Adapters from ToolSpec to provider/protocol tool schema formats."""

from __future__ import annotations

from typing import Any, Iterable

from assistant_agent.schemas.tools import ToolSpec


def tool_spec_to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """Convert ToolSpec to an OpenAI-compatible chat completions tool."""

    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": _native_description(spec),
            "parameters": _provider_schema(spec.input_schema),
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
        "inputSchema": spec.input_schema,
    }


def tool_specs_to_mcp_tools(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Convert multiple ToolSpecs to MCP-style tool definitions."""

    return [tool_spec_to_mcp_tool(spec) for spec in specs]


def _native_description(spec: ToolSpec) -> str:
    return spec.description.strip()


def _provider_schema(value: Any) -> Any:
    """Remove Pydantic presentation labels from the Provider-visible schema."""

    if isinstance(value, list):
        return [_provider_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _provider_schema(item)
        for key, item in value.items()
        if key != "title"
    }
