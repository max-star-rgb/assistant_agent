"""Read-only email Plugin, mapping, and runtime contracts."""

from typing import Any

import pytest

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.tools.plugins.builtin.email_access.models import EmailReadRequest, EmailSearchRequest
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.plugins.builtin.email_access.backend import (
    EmailMCPBinding,
    WorkspaceMCPEmailBackend,
)
from assistant_agent.tools.plugins.builtin.email_access.plugin import (
    EmailAccessPlugin,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class _RecordingRunner:
    def __init__(self, results: dict[str, ToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        self.calls.append((server_name, tool_name, tool_input))
        return self.results[tool_name]


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-email"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def _mcp_result(tool_name: str, text: str) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={"content": [{"type": "text", "text": text}]},
        model_observation={"summary": text[:20]},
        output_ref=f"mcp://gmail/{tool_name}",
    )


def _email_server_config() -> MCPServerConfig:
    return MCPServerConfig(
        server_name="gmail",
        command=["workspace-mcp", "--tools", "gmail", "--read-only"],
        allowed_tools=[
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ],
        read_only_tools=[
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ],
        email_tools={
            "search": "search_gmail_messages",
            "read_batch": "get_gmail_messages_content_batch",
            "profile": "workspace_mcp_v1",
            "user_email": "user@example.com",
        },
    )


def test_email_mapping_requires_allowlisted_read_only_workspace_tools() -> None:
    config = _email_server_config()

    assert config.email_tools.mapped_tool_names() == [
        "search_gmail_messages",
        "get_gmail_messages_content_batch",
    ]

    with pytest.raises(ValueError, match="read_only_tools"):
        MCPServerConfig(
            server_name="gmail",
            command=["workspace-mcp"],
            allowed_tools=["search_gmail_messages"],
            email_tools={
                "search": "search_gmail_messages",
                "profile": "workspace_mcp_v1",
                "user_email": "user@example.com",
            },
        )


def test_workspace_email_backend_translates_search_and_bounded_read() -> None:
    search_text = """Found 2 messages matching 'is:unread':

📧 MESSAGES:
  1. Message ID: message-1
     Web Link: https://mail.google.test/message-1
     Thread ID: thread-1
     Thread Link: https://mail.google.test/thread-1

  2. Message ID: message-2
     Web Link: https://mail.google.test/message-2
     Thread ID: thread-2
     Thread Link: https://mail.google.test/thread-2

📄 PAGINATION: To get the next page, call search_gmail_messages again with page_token='next-1'
"""
    email_body = (
        "Message ID: message-1\nSubject: Quarterly review\n"
        "From: sender@example.com\n\n--- BODY ---\n"
        "UNTRUSTED_EMAIL_BODY_SENTINEL"
    )
    runner = _RecordingRunner(
        {
            "search_gmail_messages": _mcp_result(
                "search_gmail_messages",
                search_text,
            ),
            "get_gmail_messages_content_batch": _mcp_result(
                "get_gmail_messages_content_batch",
                email_body,
            ),
        }
    )
    search_binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="search_gmail_messages",
        namespaced_tool_name="mcp.gmail.search_gmail_messages",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    read_binding = EmailMCPBinding(
        server_name="gmail",
        tool_name="get_gmail_messages_content_batch",
        namespaced_tool_name="mcp.gmail.get_gmail_messages_content_batch",
        profile="workspace_mcp_v1",
        user_email="user@example.com",
    )
    backend = WorkspaceMCPEmailBackend(
        runner=runner,
        search_binding=search_binding,
        read_binding=read_binding,
    )

    search = backend.search(
        EmailSearchRequest(query="is:unread", page_token="previous")
    )
    read = backend.read(
        EmailReadRequest(
            message_ids=["message-1"],
            max_total_chars=48,
        )
    )

    assert runner.calls[0][2] == {
        "query": "is:unread",
        "page_size": 10,
        "page_token": "previous",
        "user_google_email": "user@example.com",
    }
    assert [item.message_id for item in search.matches] == [
        "message-1",
        "message-2",
    ]
    assert search.next_page_token == "next-1"
    assert runner.calls[1][2] == {
        "message_ids": ["message-1"],
        "user_google_email": "user@example.com",
        "format": "full",
        "body_format": "text",
    }
    assert read.content_trust == "untrusted_external_content"
    assert read.instruction_policy == "do_not_execute"
    assert read.truncated is True
    assert read.original_chars == len(email_body)


def test_default_registry_owns_read_only_email_tools_and_hides_limits() -> None:
    registry = create_default_registry(plugin_modules=[])

    search = registry.get_spec("email_search")
    read = registry.get_spec("email_read")

    assert registry.registration_record("email_search").plugin_id == "email_access"
    assert registry.registration_record("email_read").plugin_id == "email_access"
    assert search.category == "read"
    assert read.category == "read"
    assert not hasattr(search, "requires_confirmation")
    assert not hasattr(read, "requires_confirmation")
    assert set(search.input_schema["properties"]) == {"query", "page_token"}
    assert set(read.input_schema["properties"]) == {"message_ids"}
    assert {
        "send_gmail_message",
        "draft_gmail_message",
        "modify_gmail_message_labels",
    }.isdisjoint(registry.list())


def test_real_email_plugin_registers_only_explicit_mapped_tools() -> None:
    runner = _RecordingRunner({})
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "local",
            "LOCAL_CHAT_BASE_URL": "http://127.0.0.1:9999/v1",
            "LOCAL_CHAT_MODEL": "local-test",
        }
    )
    plugin = EmailAccessPlugin()

    configured = plugin.build_tools(
        ToolPluginContext(
            config=config,
            mcp_server_configs=[_email_server_config()],
            mcp_runner=runner,
        )
    )
    unconfigured = plugin.build_tools(
        ToolPluginContext(
            config=config,
            mcp_server_configs=[],
            mcp_runner=runner,
        )
    )

    assert [tool.name for tool in configured] == ["email_search", "email_read"]
    assert unconfigured == []


def test_runtime_searches_reads_then_analyzes_untrusted_email() -> None:
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-email",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="email-search-call",
                        name="email_search",
                        arguments={"query": "is:unread"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-email",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="email-read-call",
                        name="email_read",
                        arguments={"message_ids": ["mock-email-1"]},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-email",
                finish_reason="stop",
                response_text="未读邮件摘要已完成。",
            ),
        ]
    )
    config = ProviderConfig(langgraph_checkpointer_backend="none")
    runtime = AgentGraphRuntime(
        config=config,
        registry=create_default_registry(config, plugin_modules=[]),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="email-user",
            session_id="email-session",
            text="总结未读邮件",
            metadata={
                "tool_visibility": {
                    "allowed_tools": ["email_search", "email_read"],
                }
            },
        )
    )

    assert state.status == "completed"
    assert state.response.message == "未读邮件摘要已完成。"
    assert [call.tool_name for call in state.tool_calls] == [
        "email_search",
        "email_read",
    ]
    rendered_third_request = str(adapter.requests[2].messages)
    assert "untrusted_external_content" in rendered_third_request
    assert "do_not_execute" in rendered_third_request
    assert "The project is on schedule" in rendered_third_request
