from assistant_agent.schemas.tool_spec_adapters import (
    tool_spec_to_json_schema,
    tool_spec_to_mcp_tool,
    tool_spec_to_openai_tool,
    tool_specs_to_mcp_tools,
    tool_specs_to_openai_tools,
)
from assistant_agent.schemas.tools import ToolExecutionPolicy, ToolSpec
from assistant_agent.tools.registry import create_default_registry


def sample_spec() -> ToolSpec:
    return ToolSpec(
        name="shopping_search",
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
    assert tool["function"]["name"] == "shopping_search"
    assert tool["function"]["parameters"]["required"] == ["query"]
    assert "User asks to find products." in tool["function"]["description"]
    assert "Use only through ToolExecutor." in tool["function"]["description"]
    assert "Side effects:" in tool["function"]["description"]
    assert "requires_confirmation=true" in tool["function"]["description"]


def test_tool_spec_to_openai_tool_adds_prompt_safe_execution_constraints_only() -> None:
    spec = ToolSpec(
        name="shopping_search",
        description="Compare prices.",
        execution=ToolExecutionPolicy(
            dependency_mode="requires_prior_observation",
            concurrency_group="catalog",
            resource_reads=["shopping_search.results"],
            resource_writes=["internal.debug"],
            realtime_safety="safe",
        ),
    )

    description = tool_spec_to_openai_tool(spec)["function"]["description"]

    assert "Execution constraints:" in description
    assert "requires prior observation" in description
    assert "resource_reads" not in description
    assert "resource_writes" not in description
    assert "concurrency_group" not in description


def test_terminal_tool_openai_description_mentions_terminal_constraint() -> None:
    spec = ToolSpec(
        name="image_generation",
        description="Generate images.",
        execution=ToolExecutionPolicy(
            dependency_mode="terminal",
            realtime_safety="needs_progress",
        ),
    )

    description = tool_spec_to_openai_tool(spec)["function"]["description"]

    assert "terminal tool" in description


def test_tool_spec_to_mcp_tool_uses_mcp_input_schema_shape() -> None:
    tool = tool_spec_to_mcp_tool(sample_spec())

    assert tool["name"] == "shopping_search"
    assert "inputSchema" in tool
    assert tool["inputSchema"]["properties"]["query"]["type"] == "string"
    assert tool["inputSchema"]["additionalProperties"] is False
    assert "Side effects:" in tool["description"]


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


def test_memory_dedicated_openai_schemas_do_not_expose_action() -> None:
    specs = {spec.name: spec for spec in create_default_registry().list_specs()}
    retrieval = tool_spec_to_openai_tool(specs["memory_retrieval"])["function"]["parameters"]
    save = tool_spec_to_openai_tool(specs["memory_save"])["function"]["parameters"]

    assert "action" not in retrieval["properties"]
    assert "action" not in save["properties"]
    assert "user_id" not in retrieval["properties"]
    assert "user_id" not in save["properties"]
    assert "session_id" not in retrieval["properties"]
    assert "session_id" not in save["properties"]
    assert retrieval["required"] == []
    assert save["required"] == []
    assert "query" in retrieval["properties"]
    assert "content" in save["properties"]
