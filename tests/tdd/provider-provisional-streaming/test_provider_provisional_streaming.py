from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.api.agent_service_websocket import _remaining_stream_text
from assistant_agent.providers.llm_events import LLMEvent, LLMToolCallDelta
from assistant_agent.runtime.chat_adapter import ChatRequest
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry


class _ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class _ProbeTool(ToolBase):
    name = "provisional_stream_probe"
    description = "Return one offline sentinel value."
    input_schema = _ProbeInput
    output_schema = _ProbeInput
    category = "read"

    def _run(self, input: _ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    registry.seal()
    return registry


class _TextStreamingAdapter:
    provider = "scripted"
    model = "streaming-model"

    async def stream_chat(self, _request: ChatRequest) -> AsyncIterator[LLMEvent]:
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text="first-",
        )
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text="second",
        )
        yield LLMEvent(
            event_type="completed",
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
        )


class _TextThenToolStreamingAdapter:
    provider = "scripted"
    model = "streaming-model"

    def __init__(
        self,
        *,
        preamble: str = "checking-",
        answer: str = "answer-sentinel",
    ) -> None:
        self.turn = 0
        self.preamble = preamble
        self.answer = answer

    async def stream_chat(self, _request: ChatRequest) -> AsyncIterator[LLMEvent]:
        self.turn += 1
        if self.turn == 1:
            yield LLMEvent(
                event_type="token_delta",
                provider=self.provider,
                model=self.model,
                text=self.preamble,
            )
            yield LLMEvent(
                event_type="tool_call_delta",
                provider=self.provider,
                model=self.model,
                tool_call_delta=LLMToolCallDelta(
                    index=0,
                    id="call-provisional",
                    name_delta=_ProbeTool.name,
                    arguments_delta='{"value":"probe',
                ),
            )
            yield LLMEvent(
                event_type="tool_call_delta",
                provider=self.provider,
                model=self.model,
                tool_call_delta=LLMToolCallDelta(
                    index=0,
                    arguments_delta='-sentinel"}',
                ),
            )
            yield LLMEvent(
                event_type="completed",
                provider=self.provider,
                model=self.model,
                finish_reason="tool_calls",
            )
            return

        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text=self.answer,
        )
        yield LLMEvent(
            event_type="completed",
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
        )


def _runtime(adapter: object) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=_registry(),
        config=ProviderConfig(
            native_provider_streaming=True,
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )


def test_text_provider_deltas_remain_live_token_events() -> None:
    sink = ListEventSink()
    runtime = _runtime(_TextStreamingAdapter())
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )
    finally:
        runtime.close()

    assert state.response is not None
    assert state.response.message == "first-second"
    deltas = [event for event in sink.events if event.type == "response_delta"]
    assert [event.text for event in deltas] == ["first-", "second"]
    assert all(event.payload["token_streaming"] is True for event in deltas)
    assert all(
        event.payload["chunking_strategy"] == "provider_token_delta"
        for event in deltas
    )


def test_text_before_tool_call_is_delivered_before_governed_tool_execution() -> None:
    sink = ListEventSink()
    runtime = _runtime(_TextThenToolStreamingAdapter())
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )
    finally:
        runtime.close()

    assert state.response is not None
    assert state.response.message == "answer-sentinel"
    assert state.tool_results[0].data == {"value": "probe-sentinel"}
    visible = [
        (event.type, event.text)
        for event in sink.events
        if event.type in {"response_delta", "tool_started"}
    ]
    assert visible[0] == ("response_delta", "checking-")
    assert [event.text for event in sink.events if event.type == "response_delta"] == [
        "checking-",
        "\nanswer-sentinel",
    ]
    assert [
        event.payload.get("runtime_separator_inserted")
        for event in sink.events
        if event.type == "response_delta"
    ] == [None, True]


def test_tool_boundary_does_not_duplicate_existing_trailing_newline() -> None:
    sink = ListEventSink()
    runtime = _runtime(
        _TextThenToolStreamingAdapter(
            preamble="checking-\n",
            answer="answer-sentinel",
        )
    )
    try:
        runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )
    finally:
        runtime.close()

    assert [event.text for event in sink.events if event.type == "response_delta"] == [
        "checking-\n",
        "answer-sentinel",
    ]


def test_tool_boundary_does_not_duplicate_existing_leading_newline() -> None:
    sink = ListEventSink()
    runtime = _runtime(
        _TextThenToolStreamingAdapter(
            preamble="checking-",
            answer="\nanswer-sentinel",
        )
    )
    try:
        runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )
    finally:
        runtime.close()

    assert [event.text for event in sink.events if event.type == "response_delta"] == [
        "checking-",
        "\nanswer-sentinel",
    ]


def test_terminal_sends_only_final_text_suffix_missing_after_provisional_iterations() -> None:
    assert (
        _remaining_stream_text(
            "answer-complete\n<detail>sentinel</detail>",
            "checking-answer-",
        )
        == "complete\n<detail>sentinel</detail>"
    )


def test_terminal_sends_full_fallback_when_provisional_text_is_not_its_prefix() -> None:
    assert _remaining_stream_text("fallback-sentinel", "partial-draft") == "fallback-sentinel"


def test_terminal_does_not_repeat_answer_normalized_from_stream_whitespace() -> None:
    assert _remaining_stream_text("answer-sentinel", " checking-answer-sentinel \n") == ""


def test_terminal_does_not_repeat_answer_after_runtime_tool_separator() -> None:
    assert _remaining_stream_text(
        "answer-sentinel",
        "checking-\nanswer-sentinel",
    ) == ""
