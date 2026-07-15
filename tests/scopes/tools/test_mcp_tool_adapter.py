from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.registry import ToolRegistry


class RecordingMCPRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> ToolResult:
        self.calls.append((server_name, tool_name, tool_input))
        return ToolResult(
            tool_name=f"mcp.{server_name}.{tool_name}",
            success=True,
            data={"summary": f"{tool_name} returned {tool_input['query']}"},
        )


def test_mcp_adapter_keeps_external_tools_disabled_without_allowlist() -> None:
    adapter = MCPToolAdapter(MCPToolAdapterConfig(server_name="calendar"))

    assert adapter.tool_spec_for_definition(_search_definition()) is None


def test_mcp_adapter_normalizes_allowed_tool_to_internal_spec() -> None:
    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name="calendar",
            allowed_tools=["search_events"],
        )
    )

    spec = adapter.tool_spec_for_definition(_search_definition())
    assert spec is not None
    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert spec.name == "mcp.calendar.search_events"
    assert spec.description == "Search calendar events."
    assert spec.required_inputs == ["query"]
    assert spec.policy is not None
    assert spec.policy.visibility.enabled_by_default is False
    assert spec.policy.visibility.toolset == "mcp.calendar"
    assert view.risk_gate_level == "hard_gate"
    assert view.requires_confirmation is True


def test_mcp_proxy_tool_runs_through_validator_executor_registry() -> None:
    runner = RecordingMCPRunner()
    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name="calendar",
            allowed_tools=["search_events"],
        ),
        runner=runner,
    )
    proxy_tool = adapter.proxy_tool_for_definition(_search_definition())
    registry = ToolRegistry()
    registry.register(proxy_tool)
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="search calendar",
        metadata={
            "tool_confirmation": {
                "tool_name": "mcp.calendar.search_events",
                "confirmed": True,
                "confirmed_by": "user",
            }
        },
    )
    state = AgentState.from_request(request, run_id="run-1")
    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="mcp.calendar.search_events",
            tool_input={"query": "standup"},
        ),
        registry=registry,
        request=request,
        state=state,
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        "mcp.calendar.search_events",
        {"query": "standup"},
    )

    assert validation.accepted is True
    assert result.success is True
    assert runner.calls == [("calendar", "search_events", {"query": "standup"})]


def _search_definition() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="search_events",
        description="Search calendar events.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
    )
