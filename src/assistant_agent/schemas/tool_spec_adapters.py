"""Adapters from ToolSpec to provider/protocol tool schema formats."""

from __future__ import annotations

from typing import Any, Iterable

from assistant_agent.schemas.tools import ToolSpec


def tool_spec_to_json_schema(spec: ToolSpec) -> dict[str, Any]:
    """Convert a ToolSpec input view to an object JSON Schema."""

    schema = spec.input_schema
    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        return _with_required(schema, spec.required_inputs)

    fields = schema.get("fields") if isinstance(schema.get("fields"), dict) else {}
    properties: dict[str, Any] = {}
    required = set(spec.required_inputs)
    for field_name, field_info in fields.items():
        if not isinstance(field_name, str) or not isinstance(field_info, dict):
            continue
        field_schema = _field_to_json_schema(field_info)
        properties[field_name] = field_schema
        if field_info.get("required") is True:
            required.add(field_name)

    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def tool_spec_to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """Convert ToolSpec to an OpenAI-compatible chat completions tool."""

    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": _native_description(spec),
            "parameters": tool_spec_to_json_schema(spec),
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
        "inputSchema": tool_spec_to_json_schema(spec),
    }


def tool_specs_to_mcp_tools(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Convert multiple ToolSpecs to MCP-style tool definitions."""

    return [tool_spec_to_mcp_tool(spec) for spec in specs]


def _with_required(schema: dict[str, Any], required_inputs: list[str]) -> dict[str, Any]:
    normalized = dict(schema)
    required = sorted({str(item) for item in [*schema.get("required", []), *required_inputs] if item})
    normalized["required"] = required
    normalized.setdefault("additionalProperties", False)
    return normalized


def _field_to_json_schema(field_info: dict[str, Any]) -> dict[str, Any]:
    field_type = str(field_info.get("type") or "string")
    if field_type not in {"string", "number", "integer", "boolean", "array", "object"}:
        field_type = "string"
    schema: dict[str, Any] = {"type": field_type}
    description = field_info.get("description")
    if isinstance(description, str) and description:
        schema["description"] = description
    if field_type == "array":
        schema.setdefault("items", {"type": "string"})
    return schema


def _native_description(spec: ToolSpec) -> str:
    parts = [spec.description.strip()] if spec.description.strip() else []
    if spec.when_to_use:
        parts.append("Use when: " + "; ".join(spec.when_to_use))
    if spec.when_not_to_use:
        parts.append("Do not use when: " + "; ".join(spec.when_not_to_use))
    if spec.runtime_constraints:
        parts.append("Runtime constraints: " + "; ".join(spec.runtime_constraints))
    return "\n".join(parts)
