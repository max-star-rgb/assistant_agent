from multimodal_agent.schemas.tool_spec_adapters import (
    tool_spec_to_json_schema,
    tool_spec_to_mcp_tool,
    tool_spec_to_openai_tool,
    tool_specs_to_mcp_tools,
    tool_specs_to_openai_tools,
)
from multimodal_agent.schemas.tools import ToolSpec
from multimodal_agent.tools.registry import create_default_registry


def sample_spec() -> ToolSpec:
    return ToolSpec(
        name="product_search",
        description="Search products.",
        input_schema={
            "fields": {
                "query": {"type": "string", "description": "Search query.", "required": True},
                "limit": {"type": "integer", "description": "Max results.", "required": False},
                "tags": {"type": "array", "description": "Optional tags.", "required": False},
            }
        },
        required_inputs=["query"],
        when_to_use=["User asks to find products."],
        when_not_to_use=["User only asks for general chat."],
        runtime_constraints=["Use only through ToolExecutor."],
    )


def test_tool_spec_to_json_schema_converts_legacy_fields_view() -> None:
    schema = tool_spec_to_json_schema(sample_spec())

    assert schema == {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Max results."},
            "tags": {"type": "array", "description": "Optional tags.", "items": {"type": "string"}},
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def test_tool_spec_to_json_schema_preserves_object_schema_and_merges_required() -> None:
    spec = ToolSpec(
        name="memory_save",
        input_schema={
            "type": "object",
            "properties": {"content": {"type": "string"}, "user_id": {"type": "string"}},
            "required": ["content"],
        },
        required_inputs=["user_id"],
    )

    schema = tool_spec_to_json_schema(spec)

    assert schema["properties"]["content"] == {"type": "string"}
    assert schema["required"] == ["content", "user_id"]
    assert schema["additionalProperties"] is False


def test_tool_spec_to_openai_tool_uses_function_calling_shape() -> None:
    tool = tool_spec_to_openai_tool(sample_spec())

    assert tool["type"] == "function"
    assert tool["function"]["name"] == "product_search"
    assert tool["function"]["parameters"]["required"] == ["query"]
    assert "User asks to find products." in tool["function"]["description"]
    assert "Use only through ToolExecutor." in tool["function"]["description"]


def test_tool_spec_to_mcp_tool_uses_mcp_input_schema_shape() -> None:
    tool = tool_spec_to_mcp_tool(sample_spec())

    assert tool["name"] == "product_search"
    assert "inputSchema" in tool
    assert tool["inputSchema"]["properties"]["query"]["type"] == "string"
    assert tool["inputSchema"]["additionalProperties"] is False


def test_tool_spec_batch_converters_preserve_order() -> None:
    specs = [
        ToolSpec(name="a", input_schema={"fields": {}}),
        ToolSpec(name="b", input_schema={"fields": {}}),
    ]

    assert [tool["function"]["name"] for tool in tool_specs_to_openai_tools(specs)] == ["a", "b"]
    assert [tool["name"] for tool in tool_specs_to_mcp_tools(specs)] == ["a", "b"]


def test_default_registry_specs_convert_without_secrets() -> None:
    specs = create_default_registry().list_specs()
    openai_tools = tool_specs_to_openai_tools(specs)
    mcp_tools = tool_specs_to_mcp_tools(specs)

    assert len(openai_tools) == len(specs)
    assert len(mcp_tools) == len(specs)
    payload = str({"openai_tools": openai_tools, "mcp_tools": mcp_tools}).lower()
    assert "api_key" not in payload
    assert "authorization" not in payload
    assert "bearer" not in payload
