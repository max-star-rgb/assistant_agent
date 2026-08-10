from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.mcp.sdk_client import SdkMCPClientRunner, _sdk_session
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.observation import (
    observation_from_tool_result,
    prompt_observation_payload,
)
from assistant_agent.tools.plugins.builtin.email_access.backend import (
    EmailMCPBinding,
    WorkspaceMCPEmailBackend,
)
from assistant_agent.tools.plugins.builtin.email_access.models import (
    EmailReadRequest,
    EmailSearchRequest,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    EmailReadTool,
    EmailSearchTool,
)
from assistant_agent.tools.registry import ToolRegistry
from tests.core.support import ScriptedChatAdapter


class _TimeoutRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> ToolResult:
        return ToolResult(
            tool_name=f"mcp.{server_name}.{tool_name}",
            success=False,
            error="MCP server response timed out.",
            output_ref=f"mcp://{server_name}/{tool_name}",
        )


class _AuthFailureRunner:
    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> ToolResult:
        return ToolResult(
            tool_name=f"mcp.{server_name}.{tool_name}",
            success=False,
            error=(
                "Error calling tool 'search_gmail_messages': "
                "ACTION REQUIRED: Google Authentication Needed for Google Gmail."
            ),
        )


def _server(*, timeout_seconds: float = 0.25) -> MCPServerConfig:
    return MCPServerConfig(
        server_name="gmail",
        command=["workspace-mcp"],
        allowed_tools=["search_gmail_messages"],
        read_only_tools=["search_gmail_messages"],
        timeout_seconds=timeout_seconds,
    )


def test_sdk_call_tool_receives_configured_read_timeout(monkeypatch) -> None:
    received: list[timedelta | None] = []
    deadlines: list[float] = []

    class _Session:
        async def call_tool(
            self,
            name: str,
            *,
            arguments: dict[str, object],
            read_timeout_seconds: timedelta | None = None,
        ) -> object:
            received.append(read_timeout_seconds)
            return {"content": [{"type": "text", "text": "ok"}]}

    @asynccontextmanager
    async def _session(_server: MCPServerConfig, *, deadline: float):
        deadlines.append(deadline)
        await asyncio.sleep(0.05)
        yield _Session()

    monkeypatch.setattr("assistant_agent.mcp.sdk_client._sdk_session", _session)
    runner = SdkMCPClientRunner([_server()])

    result = asyncio.run(
        runner._run_tool(
            server=_server(),
            tool_name="search_gmail_messages",
            namespaced_tool_name="mcp.gmail.search_gmail_messages",
            tool_input={"query": "sentinel"},
        )
    )

    assert result.success is True
    assert len(deadlines) == 1
    assert len(received) == 1
    assert received[0] is not None
    assert 0.15 <= received[0].total_seconds() < 0.25


def test_sdk_session_scopes_deadline_to_initialize_for_safe_cleanup(monkeypatch) -> None:
    @asynccontextmanager
    async def _stdio_client(_params, *, errlog):
        yield object(), object()

    class _ClientSession:
        def __init__(self, _read, _write) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr("mcp.client.stdio.stdio_client", _stdio_client)
    monkeypatch.setattr("mcp.ClientSession", _ClientSession)

    async def _exercise() -> str:
        deadline = asyncio.get_running_loop().time() + 0.01
        async with _sdk_session(_server(timeout_seconds=0.01), deadline=deadline):
            await asyncio.sleep(0.03)
        return "completed"

    assert asyncio.run(_exercise()) == "completed"


def test_sdk_runner_preserves_nested_timeout_reason(monkeypatch) -> None:
    def _raise_nested_timeout(_factory):
        raise ExceptionGroup(
            "MCP transport cleanup failed",
            [TimeoutError("MCP server response timed out.")],
        )

    monkeypatch.setattr(
        "assistant_agent.mcp.sdk_client._run_async_from_sync",
        _raise_nested_timeout,
    )
    runner = SdkMCPClientRunner([_server()])

    result = runner.run_tool(
        server_name="gmail",
        tool_name="search_gmail_messages",
        tool_input={"query": "sentinel"},
    )

    assert result.success is False
    assert result.error == "provider_timeout: MCP server response timed out."


def test_public_sdk_runner_times_out_hanging_stdio_tool(tmp_path) -> None:
    server_script = tmp_path / "hanging_mcp_server.py"
    server_script.write_text(
        """
import json
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "hanging-test", "version": "1"},
            },
        }
        print(json.dumps(response), flush=True)
    elif method == "tools/call":
        time.sleep(10)
""".strip(),
        encoding="utf-8",
    )
    server = MCPServerConfig(
        server_name="gmail",
        command=[sys.executable, str(server_script)],
        allowed_tools=["search_gmail_messages"],
        read_only_tools=["search_gmail_messages"],
        timeout_seconds=0.1,
    )
    runner = SdkMCPClientRunner([server])

    started = time.monotonic()
    result = runner.run_tool(
        server_name="gmail",
        tool_name="search_gmail_messages",
        tool_input={"query": "sentinel"},
    )
    elapsed = time.monotonic() - started

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("provider_timeout:")
    assert elapsed < 4.0


def test_email_timeout_becomes_llm_visible_failed_observation() -> None:
    binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="search_gmail_messages",
        namespaced_tool_name="mcp.gmail.search_gmail_messages",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    backend = WorkspaceMCPEmailBackend(
        runner=_TimeoutRunner(),
        search_binding=binding,
        read_binding=None,
    )

    result = backend.search(EmailSearchRequest(query="sentinel", limit=10))
    tool_result = ToolResult(
        tool_name="email_search",
        success=result.success,
        model_observation={
            "summary": result.summary,
            "errors": [item.model_dump(mode="json") for item in result.errors],
        },
        error=result.errors[0].message,
    )
    payload = prompt_observation_payload(
        observation_from_tool_result(tool_result).model_dump(mode="json")
    )

    assert result.success is False
    assert result.errors[0].code == "provider_timeout"
    assert payload["status"] == "failed"
    assert payload["error"] == {
        "code": "provider_timeout",
        "message": "MCP server response timed out.",
        "retryable": True,
    }


def test_email_read_timeout_becomes_llm_visible_failed_observation() -> None:
    binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="get_gmail_messages_content_batch",
        namespaced_tool_name="mcp.gmail.get_gmail_messages_content_batch",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    tool = EmailReadTool(
        WorkspaceMCPEmailBackend(
            runner=_TimeoutRunner(),
            search_binding=None,
            read_binding=binding,
        )
    )

    result = tool.run(EmailReadRequest(message_ids=["message-sentinel"]))
    payload = prompt_observation_payload(
        observation_from_tool_result(result).model_dump(mode="json")
    )

    assert result.success is False
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "provider_timeout"
    assert payload["error"]["retryable"] is True


def test_email_auth_failure_is_not_reported_as_recoverable() -> None:
    binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="search_gmail_messages",
        namespaced_tool_name="mcp.gmail.search_gmail_messages",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    backend = WorkspaceMCPEmailBackend(
        runner=_AuthFailureRunner(),
        search_binding=binding,
        read_binding=None,
    )

    result = backend.search(EmailSearchRequest(query="sentinel"))

    assert result.success is False
    assert result.errors[0].code == "provider_auth_failed"
    assert result.errors[0].recoverable is False


def test_assistant_loop_turns_email_failure_into_completed_llm_answer() -> None:
    binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="search_gmail_messages",
        namespaced_tool_name="mcp.gmail.search_gmail_messages",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    registry = ToolRegistry()
    registry.register(
        EmailSearchTool(
            WorkspaceMCPEmailBackend(
                runner=_TimeoutRunner(),
                search_binding=binding,
                read_binding=None,
            )
        )
    )
    registry.seal()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-email-timeout",
                        name="email_search",
                        arguments={"query": "sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="邮件服务连接超时，本次未能完成查询。",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )
    finally:
        runtime.close()

    tool_message = next(
        message
        for message in adapter.requests[1].messages
        if message.get("role") == "tool"
    )
    observation = json.loads(str(tool_message["content"]))
    assert observation["status"] == "failed", observation
    assert observation["error"]["code"] == "provider_timeout"
    assert state.status == "completed"
    assert state.response is not None
    assert state.response.message == "邮件服务连接超时，本次未能完成查询。"
