"""Smoke test the SDK-backed MCP client with a local FastMCP tool server."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.tools.registry import create_default_registry


_SERVER_SCRIPT = r'''
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Assistant Agent Smoke MCP Server")


@mcp.tool()
def echo(query: str) -> dict[str, object]:
    """Echo a query through an external MCP tool."""

    return {
        "summary": f"echo: {query}",
        "provider_raw_response": {"api_key": "qwen-secret"},
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="assistant-agent-mcp-") as tmpdir:
        server_script = Path(tmpdir) / "fastmcp_echo_server.py"
        server_script.write_text(_SERVER_SCRIPT, encoding="utf-8")
        server = MCPServerConfig(
            server_name="smoke",
            command=[sys.executable, str(server_script)],
            allowed_tools=["echo"],
            read_only_tools=["echo"],
            enabled_tools=["echo"],
            timeout_seconds=5,
        )
        registry = create_default_registry(
            enable_mcp_tools=True,
            mcp_server_configs=[server],
        )
        request = UserRequest(user_id="smoke-user", session_id="smoke-session", text="echo")
        state = AgentState.from_request(request)
        decision = AssistantDecision(
            type="tool_call",
            tool_name="mcp.smoke.echo",
            tool_input={"query": "hello mcp sdk"},
        )
        validation = ActionValidator().validate(
            decision=decision,
            registry=registry,
            request=request,
            state=state,
        )
        if not validation.accepted:
            print(
                json.dumps(
                    {
                        "accepted": False,
                        "reason": validation.reason,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        result = ToolExecutor(registry=registry).run_tool(
            state,
            "mcp-smoke-step",
            "mcp.smoke.echo",
            {"query": "hello mcp sdk"},
        )
        payload = {
            "registered_mcp_tools": [
                name for name in registry.list() if name.startswith("mcp.")
            ],
            "accepted": validation.accepted,
            "success": result.success,
            "tool_name": result.tool_name,
            "model_observation": result.model_observation,
            "error": result.error,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
