import json
import sys
from pathlib import Path

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.mcp.adapter import MCPToolDefinition
from assistant_agent.mcp.config import MCPServerConfig, load_mcp_server_configs_from_env
from assistant_agent.mcp.registration import register_configured_mcp_tools
from assistant_agent.mcp.sdk_client import SdkMCPClientRunner
from assistant_agent.mcp.stdio_client import StdioMCPClientRunner
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class RecordingMCPRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def list_tools(self, *, server: MCPServerConfig) -> list[MCPToolDefinition]:
        assert server.server_name == "calendar"
        return [
            MCPToolDefinition(
                name="search_events",
                description="Search calendar events.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query.",
                        }
                    },
                    "required": ["query"],
                },
            ),
            MCPToolDefinition(
                name="delete_event",
                description="Delete calendar event.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                    },
                    "required": ["event_id"],
                },
            ),
        ]

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
            model_observation={"summary": f"calendar result for {tool_input['query']}"},
        )


def test_mcp_config_loader_requires_explicit_enable_and_file(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_name": "calendar",
                        "transport": "stdio",
                        "command": ["/usr/bin/calendar-mcp"],
                        "allowed_tools": ["search_events", "delete_event"],
                        "read_only_tools": ["search_events"],
                        "enabled_tools": ["search_events"],
                        "timeout_seconds": 3.5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_mcp_server_configs_from_env(
        {"MULTIMODAL_AGENT_MCP_CONFIG_PATH": str(config_path)}
    ) == []

    configs = load_mcp_server_configs_from_env(
        {
            "MULTIMODAL_AGENT_MCP_ENABLED": "1",
            "MULTIMODAL_AGENT_MCP_CONFIG_PATH": str(config_path),
        }
    )

    assert len(configs) == 1
    assert configs[0].server_name == "calendar"
    assert configs[0].allowed_tools == ["search_events", "delete_event"]
    assert configs[0].read_only_tools == ["search_events"]
    assert configs[0].enabled_tools == ["search_events"]
    assert configs[0].timeout_seconds == 3.5


def test_mcp_config_loader_skips_invalid_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text("{bad json", encoding="utf-8")

    assert (
        load_mcp_server_configs_from_env(
            {
                "MULTIMODAL_AGENT_MCP_ENABLED": "1",
                "MULTIMODAL_AGENT_MCP_CONFIG_PATH": str(config_path),
            }
        )
        == []
    )

    config_path.write_text(
        json.dumps({"servers": [{"server_name": "broken", "command": []}]}),
        encoding="utf-8",
    )

    assert (
        load_mcp_server_configs_from_env(
            {
                "MULTIMODAL_AGENT_MCP_ENABLED": "1",
                "MULTIMODAL_AGENT_MCP_CONFIG_PATH": str(config_path),
            }
        )
        == []
    )


def test_register_configured_mcp_tools_allowlists_and_governs_external_tools() -> None:
    registry = ToolRegistry()
    runner = RecordingMCPRunner()

    summary = register_configured_mcp_tools(
        registry,
        [
            MCPServerConfig(
                server_name="calendar",
                command=["calendar-mcp"],
                allowed_tools=["search_events"],
                read_only_tools=["search_events"],
                enabled_tools=["search_events"],
            )
        ],
        runner=runner,
    )

    assert summary.registered_tool_names == ["mcp.calendar.search_events"]
    assert summary.skipped_tool_names == ["mcp.calendar.delete_event"]
    assert summary.issues == []
    assert registry.list() == ["mcp.calendar.search_events"]

    spec = registry.get_spec("mcp.calendar.search_events")
    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert spec.required_inputs == ["query"]
    assert view.side_effect_level == "external_read"
    assert view.risk_gate_level == "auto"
    assert view.requires_confirmation is False
    assert view.enabled_by_default is True
    assert view.toolset == "mcp.calendar"
    assert view.resource_reads == ["mcp.calendar.search_events"]
    assert view.dependency_mode == "independent"


def test_registered_mcp_proxy_runs_through_validator_executor_registry() -> None:
    registry = ToolRegistry()
    runner = RecordingMCPRunner()
    register_configured_mcp_tools(
        registry,
        [
            MCPServerConfig(
                server_name="calendar",
                command=["calendar-mcp"],
                allowed_tools=["search_events"],
                read_only_tools=["search_events"],
                enabled_tools=["search_events"],
            )
        ],
        runner=runner,
    )
    request = UserRequest(user_id="u1", session_id="s1", text="search calendar")
    state = AgentState.from_request(request)
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
    assert result.model_observation == {"summary": "calendar result for standup"}
    assert runner.calls == [("calendar", "search_events", {"query": "standup"})]


def test_default_registry_can_opt_in_configured_mcp_tools() -> None:
    runner = RecordingMCPRunner()
    registry = create_default_registry(
        enable_mcp_tools=True,
        mcp_server_configs=[
            MCPServerConfig(
                server_name="calendar",
                command=["calendar-mcp"],
                allowed_tools=["search_events"],
                read_only_tools=["search_events"],
                enabled_tools=["search_events"],
            )
        ],
        mcp_runner=runner,
    )

    assert "mcp.calendar.search_events" in registry.list()
    assert "mcp.calendar.delete_event" not in registry.list()


def test_stdio_mcp_client_runner_rejects_unallowlisted_direct_call() -> None:
    server = MCPServerConfig(
        server_name="fake",
        command=["missing-mcp-server"],
        allowed_tools=["echo"],
    )
    result = StdioMCPClientRunner([server]).run_tool(
        server_name="fake",
        tool_name="delete_event",
        tool_input={},
    )

    assert result.success is False
    assert result.tool_name == "mcp.fake.delete_event"
    assert "allowlisted" in (result.error or "")


def test_stdio_mcp_client_runner_lists_and_calls_fake_server(tmp_path: Path) -> None:
    server_script = tmp_path / "fake_mcp_server.py"
    server_script.write_text(_FAKE_MCP_SERVER_SCRIPT, encoding="utf-8")
    server = MCPServerConfig(
        server_name="fake",
        command=[sys.executable, str(server_script)],
        allowed_tools=["echo"],
        read_only_tools=["echo"],
        enabled_tools=["echo"],
        timeout_seconds=3,
    )
    runner = StdioMCPClientRunner([server])

    definitions = runner.list_tools(server=server)
    result = runner.run_tool(
        server_name="fake",
        tool_name="echo",
        tool_input={"query": "hello"},
    )

    assert [definition.name for definition in definitions] == ["echo"]
    assert definitions[0].input_schema["required"] == ["query"]
    assert result.success is True
    assert result.tool_name == "mcp.fake.echo"
    assert result.model_observation == {"summary": "echo: hello"}
    prompt_visible_payload = {
        "data": result.data,
        "model_observation": result.model_observation,
        "error": result.error,
    }
    serialized_payload = json.dumps(prompt_visible_payload).lower()
    assert "raw" not in serialized_payload
    assert "qwen-" not in serialized_payload


def test_sdk_mcp_client_runner_lists_and_calls_fake_server(tmp_path: Path) -> None:
    server_script = tmp_path / "fastmcp_server.py"
    server_script.write_text(_FASTMCP_SERVER_SCRIPT, encoding="utf-8")
    server = MCPServerConfig(
        server_name="fake",
        command=[sys.executable, str(server_script)],
        allowed_tools=["echo"],
        read_only_tools=["echo"],
        enabled_tools=["echo"],
        timeout_seconds=3,
    )
    runner = SdkMCPClientRunner([server])

    definitions = runner.list_tools(server=server)
    result = runner.run_tool(
        server_name="fake",
        tool_name="echo",
        tool_input={"query": "hello from sdk"},
    )

    assert [definition.name for definition in definitions] == ["echo"]
    assert definitions[0].input_schema["required"] == ["query"]
    assert result.success is True
    assert result.tool_name == "mcp.fake.echo"
    assert result.model_observation == {"summary": "echo: hello from sdk"}
    serialized_payload = json.dumps(
        {
            "data": result.data,
            "model_observation": result.model_observation,
            "error": result.error,
        }
    ).lower()
    assert "raw" not in serialized_payload
    assert "qwen-" not in serialized_payload


def test_default_mcp_registration_uses_sdk_client_runner(tmp_path: Path) -> None:
    server_script = tmp_path / "fastmcp_server.py"
    server_script.write_text(_FASTMCP_SERVER_SCRIPT, encoding="utf-8")
    server = MCPServerConfig(
        server_name="fake",
        command=[sys.executable, str(server_script)],
        allowed_tools=["echo"],
        read_only_tools=["echo"],
        enabled_tools=["echo"],
        timeout_seconds=3,
    )
    registry = ToolRegistry()

    summary = register_configured_mcp_tools(registry, [server])
    result = registry.run("mcp.fake.echo", {"query": "default sdk"})

    assert summary.registered_tool_names == ["mcp.fake.echo"]
    assert summary.issues == []
    assert result.success is True
    assert result.model_observation == {"summary": "echo: default sdk"}


_FAKE_MCP_SERVER_SCRIPT = r'''
import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def write_message(payload):
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0"},
            },
        })
    elif method == "tools/list":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo query.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]
            },
        })
    elif method == "tools/call":
        query = message["params"]["arguments"]["query"]
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "content": [{"type": "text", "text": f"echo: {query}"}],
                "structuredContent": {
                    "summary": f"echo: {query}",
                    "provider_raw_response": {"api_key": "qwen-secret"},
                },
            },
        })
'''


_FASTMCP_SERVER_SCRIPT = r'''
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Fake SDK MCP Server")


@mcp.tool()
def echo(query: str) -> dict[str, object]:
    """Echo query."""

    return {
        "summary": f"echo: {query}",
        "provider_raw_response": {"api_key": "qwen-secret"},
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''
