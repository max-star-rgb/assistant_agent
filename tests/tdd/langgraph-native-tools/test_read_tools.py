from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    MockCalendarAdapter,
    MockContactsAdapter,
    UnconfiguredCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    create_calendar_create_tool,
    create_calendar_search_tool,
    create_contacts_search_tool,
)
from assistant_agent.tools.plugins.builtin.email_access.backend import MockEmailBackend
from assistant_agent.tools.plugins.builtin.email_access.models import (
    EmailProviderError,
    EmailSearchRequest,
    EmailSearchResult,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    create_email_read_tool,
    create_email_search_tool,
)
READ_TOOL_MODULES = (
    "assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools",
    "assistant_agent.tools.plugins.builtin.email_access.tools",
)


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _FailingEmailBackend:
    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        return EmailSearchResult(
            success=False,
            query_used=request.query,
            summary="Mailbox unavailable.",
            provider="email-error",
            output_ref="error://email/search",
            errors=[EmailProviderError(code="email_unavailable", message="Mailbox unavailable.")],
        )

    def read(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("email_read is not used by this test")


class _FailedWithoutErrorsEmailBackend:
    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        return EmailSearchResult(
            success=False,
            query_used=request.query,
            summary="Mailbox failed without provider detail.",
            provider="email-error",
            output_ref="error://email/search",
        )

    def read(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("email_read is not used by this test")


def test_calendar_and_contacts_tools_return_native_content_and_artifact() -> None:
    calendar = _invoke(create_calendar_search_tool(MockCalendarAdapter()), {"query": "today"})
    created = _invoke(
        create_calendar_create_tool(MockCalendarAdapter()),
        {"title": "Native meeting", "start_time": "2026-09-02T09:00:00+08:00"},
    )
    contacts = _invoke(create_contacts_search_tool(MockContactsAdapter()), {"query": "Alex"})

    assert json.loads(calendar.content[0]["text"]) == {
        "events": [
            {
                "attendee_count": 3,
                "end_time": "2026-07-20T10:30:00+08:00",
                "event_id": "mock-calendar-product-sync",
                "location": "Conference Room A",
                "start_time": "2026-07-20T10:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "title": "Product sync",
            }
        ],
        "provider": "mock",
        "query_used": "today",
        "summary": "Calendar search returned 1 event(s).",
    }
    assert calendar.artifact == {
        "success": True,
        "query_used": "today",
        "events": [
            {
                "event_id": "mock-calendar-product-sync",
                "title": "Product sync",
                "start_time": "2026-07-20T10:00:00+08:00",
                "end_time": "2026-07-20T10:30:00+08:00",
                "timezone": "Asia/Shanghai",
                "location": "Conference Room A",
                "attendee_count": 3,
            }
        ],
        "summary": "Calendar search returned 1 event(s).",
        "provider": "mock",
        "latency_ms": 1,
        "output_ref": "mock://calendar/search/today",
        "raw_data_ref": "mock://calendar/events/today",
        "errors": [],
    }
    assert calendar.status == "success"
    assert json.loads(created.content[0]["text"])["event_id"] == "mock-calendar-native:thread:run:call-calendar_create"
    assert created.artifact["idempotency"] == {
        "key": "native:thread:run:call-calendar_create",
        "present": True,
        "required": True,
    }
    assert created.status == "success"
    assert json.loads(contacts.content[0]["text"])["contacts"][0]["contact_id"] == "mock-contact-alex"
    assert contacts.artifact["success"] is True
    assert contacts.status == "success"


def test_email_tools_return_native_content_and_artifact() -> None:
    search = _invoke(create_email_search_tool(MockEmailBackend()), {"query": "project"})
    read = _invoke(create_email_read_tool(MockEmailBackend()), {"message_ids": ["mock-email-1"]})

    assert json.loads(search.content[0]["text"]) == {
        "matches": [{"message_id": "mock-email-1", "thread_id": "mock-thread-1"}],
        "provider": "mock",
        "query_used": "project",
        "summary": "Mock mailbox search returned 1 message.",
    }
    assert search.artifact["success"] is True
    assert search.status == "success"
    assert json.loads(read.content[0]["text"])["content_trust"] == "untrusted_external_content"
    assert read.artifact["message_ids"] == ["mock-email-1"]
    assert read.status == "success"


def test_failed_result_without_errors_becomes_toolnode_error() -> None:
    message = _invoke(
        create_email_search_tool(_FailedWithoutErrorsEmailBackend()),
        {"query": "project"},
    )

    assert message.content == "provider_error: email_search failed"
    assert message.artifact is None
    assert message.status == "error"


@pytest.mark.parametrize(
    ("tool", "args", "expected_error"),
    [
        (
            create_calendar_search_tool(UnconfiguredCalendarAdapter("calendar-error", "CALENDAR_TOKEN")),
            {"query": "today"},
            "provider_unconfigured: calendar-error calendar provider is missing CALENDAR_TOKEN.",
        ),
        (
            create_email_search_tool(_FailingEmailBackend()),
            {"query": "project"},
            "email_unavailable: Mailbox unavailable.",
        ),
    ],
)
def test_read_tool_failures_become_toolnode_error_messages(
    tool: BaseTool,
    args: dict[str, object],
    expected_error: str,
) -> None:
    message = _invoke(tool, args)

    assert message.content == expected_error
    assert message.artifact is None
    assert message.status == "error"


@pytest.mark.parametrize("module_name", READ_TOOL_MODULES)
def test_read_tool_modules_do_not_import_compatibility_execution_types(module_name: str) -> None:
    imported = imported_names(module_name)

    assert "ToolContext" not in imported
    assert "ToolResult" not in imported
    assert "invoke_native_tool" not in imported


def _invoke(tool: BaseTool, args: dict[str, object]) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool], handle_tool_errors=lambda error: str(error)))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = asyncio.run(
        builder.compile().ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": tool.name,
                                "args": args,
                                "id": f"call-{tool.name}",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "thread_id": "thread",
                    "run_id": "run",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message


def imported_names(module_name: str) -> set[str]:
    module_path = Path(__file__).parents[3] / "src" / (module_name.replace(".", "/") + ".py")
    names: set[str] = set()
    for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names
