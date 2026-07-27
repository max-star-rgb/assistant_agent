"""Regression coverage for compiled prompt context accounting."""

import json

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_store import InMemoryTraceStore


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


def test_context_report_accounts_for_the_compiled_chat_request() -> None:
    adapter = _CapturedChatAdapter()
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
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
    assistant_event = next(
        event
        for event in trace_store.list_by_run(state.run_id)
        if event.canonical_event == "assistant.output"
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
    assert "context" not in assistant_event.output_summary
    assert "context_report_v1" not in assistant_event.output_summary

    spans = build_text_otel_span_specs(trace_store.list_by_run(state.run_id))
    runtime_span = next(span for span in spans if span.name == "assistant.runtime")
    context_span = next(span for span in spans if span.name == "context.build")
    runtime_context_keys = {
        key for key in runtime_span.attributes if "context" in key.lower()
    }
    assert runtime_context_keys == {"langfuse.trace.metadata.context_peak_ratio"}
    context_output = json.loads(
        context_span.attributes["langfuse.observation.output"]
    )
    assert context_output["context_report_v1"] == report
