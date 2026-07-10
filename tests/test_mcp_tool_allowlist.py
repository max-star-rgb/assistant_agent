from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPToolAdapterConfig


def test_mcp_adapter_rejects_non_allowlisted_tool() -> None:
    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name="calendar",
            allowed_tools=["search_events"],
        )
    )

    spec = adapter.tool_spec_for_definition(
        MCPToolDefinition(
            name="delete_event",
            description="Delete a calendar event.",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
    )

    assert spec is None


def test_mcp_adapter_namespaces_and_sanitizes_external_names() -> None:
    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name="work calendar",
            allowed_tools=["search events"],
        )
    )

    spec = adapter.tool_spec_for_definition(
        MCPToolDefinition(
            name="search events",
            description="Search calendar events.",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
    )

    assert spec is not None
    assert spec.name == "mcp.work_calendar.search_events"
