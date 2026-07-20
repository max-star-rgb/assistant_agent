import json

from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.tool_search_tool import ToolSearchTool


class SearchDiscoveryRunner:
    def __init__(self) -> None:
        self.listed_servers: list[str] = []

    def list_tools(self, *, server: MCPServerConfig) -> list[dict[str, object]]:
        self.listed_servers.append(server.server_name)
        return [
            {
                "name": "search_files",
                "description": "Search workspace files.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "send_email",
                "description": "Send an email message to a contact.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "delete_everything",
                "description": "Dangerous unallowlisted operation.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        ]


def test_tool_search_discovers_mcp_tools_that_need_permission() -> None:
    runner = SearchDiscoveryRunner()
    tool = ToolSearchTool(
        server_configs=[
            MCPServerConfig(
                server_name="workspace",
                command=["workspace-mcp"],
                allowed_tools=["search_files", "send_email"],
                read_only_tools=["search_files"],
                enabled_tools=["search_files"],
            )
        ],
        runner=runner,
    )

    result = tool.run({"query": "send an email", "limit": 5})

    assert result.success is True
    assert runner.listed_servers == ["workspace"]
    assert result.model_observation is not None
    assert result.model_observation["matches"][0]["tool_name"] == "mcp.workspace.send_email"
    assert result.model_observation["matches"][0]["status"] == "permission_required"
    assert result.model_observation["matches"][0]["permission_required"] is True
    assert result.model_observation["matches"][0]["required_inputs"] == [
        "to",
        "subject",
        "body",
    ]
    assert "tool_visibility.enabled_tools" in result.model_observation["matches"][0]["permission_hint"]
    serialized = json.dumps(result.model_observation, ensure_ascii=False)
    assert "delete_everything" not in serialized
    assert result.data is not None
    assert result.data["omitted_unallowlisted_count"] == 1


def test_tool_search_reports_enabled_mcp_tools_without_permission_gate() -> None:
    runner = SearchDiscoveryRunner()
    tool = ToolSearchTool(
        server_configs=[
            MCPServerConfig(
                server_name="workspace",
                command=["workspace-mcp"],
                allowed_tools=["search_files", "send_email"],
                read_only_tools=["search_files"],
                enabled_tools=["search_files"],
            )
        ],
        runner=runner,
    )

    result = tool.run({"query": "files", "limit": 5})

    assert result.success is True
    assert result.model_observation is not None
    match = result.model_observation["matches"][0]
    assert match["tool_name"] == "mcp.workspace.search_files"
    assert match["status"] == "enabled"
    assert match["permission_required"] is False
    assert match["side_effect_level"] == "external_read"
    assert match["read_only"] is True


def test_default_registry_registers_tool_search_as_local_read_discovery_tool() -> None:
    registry = create_default_registry()

    assert "tool_search" in registry.list()
    spec = registry.get_spec("tool_search")
    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert view.side_effect_level == "local_read"
    assert view.risk_gate_level == "auto"
    assert view.requires_confirmation is False
    assert view.dependency_mode == "requires_prior_observation"
    assert "MCP" in spec.description
