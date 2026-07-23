"""Regression coverage for compiled prompt context accounting."""

import json
from datetime import datetime, timedelta, timezone

from assistant_agent.agent.system_prompt_policy import render_system_instruction
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import InMemoryTraceStore


class _CapturedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="完成。",
        )


def _json_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def test_system_instruction_contains_trusted_runtime_time() -> None:
    current_time = datetime(
        2026,
        7,
        22,
        16,
        30,
        5,
        tzinfo=timezone(timedelta(hours=8)),
    )

    instruction = render_system_instruction(current_time=current_time)

    assert instruction.startswith("# 运行时事实\n\n")
    assert "当前本地时间：2026-07-22T16:30:05+08:00" in instruction
    assert "可信事实" in instruction
    assert "相对日期" in instruction
    assert instruction.index("# 运行时事实") < instruction.index("# 角色")


def test_context_report_accounts_for_the_compiled_chat_request() -> None:
    adapter = _CapturedChatAdapter()
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
        trace_store=trace_store,
    )

    state = runtime.run_state(
        UserRequest(user_id="context-user", session_id="context-session", text="帮我完成任务")
    )

    request = adapter.requests[0]
    context_event = next(
        event
        for event in trace_store.list_by_run(state.run_id)
        if event.canonical_event == "context.build.finished"
    )
    report = context_event.output_summary["context_report_v1"]
    message_chars = _json_chars(request.messages)
    tool_chars = _json_chars(request.tools)
    response_format_chars = 0

    assert report["accounting_basis"] == "compiled_chat_request"
    assert report["sections"]["system_prompt"]["chars"] == len(request.messages[0]["content"])
    assert report["sections"]["tool_schema"]["chars"] == tool_chars
    assert report["compiled_message_chars"] == message_chars
    assert report["compiled_tool_schema_chars"] == tool_chars
    assert request.response_format is None
    assert report["compiled_response_format_chars"] == response_format_chars
    assert report["total_chars"] == message_chars + tool_chars + response_format_chars
    assert report["budget_estimated_chars"] == context_event.output_summary["context"]["budget"][
        "total_chars"
    ]
