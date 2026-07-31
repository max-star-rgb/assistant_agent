"""Regression for ReAct-owned realtime response delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from assistant_agent.gateway.runtime_adapter import (
    GatewayRuntimeAdapter,
    realtime_request_to_user_request,
)
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.observability.trace_store import InMemoryTraceStore


def test_realtime_entry_defaults_to_voice_response_style() -> None:
    request = realtime_request_to_user_request(
        RealtimeAgentRequest(
            user_id="voice-user",
            session_id="voice-session",
            text="继续说",
        )
    )

    assert request.response_style == "voice"


def test_realtime_delivers_llm_final_text_without_tool_result_override() -> None:
    asyncio.run(_assert_realtime_delivers_llm_final_text_without_tool_result_override())


async def _assert_realtime_delivers_llm_final_text_without_tool_result_override() -> None:
    trace_store = InMemoryTraceStore()
    final_text = (
        "已找到商品。\n<detail>\n"
        "1. 淘宝 - 草莓牛奶 12元 <link>https://example.com/product</link> "
        "<pic>https://example.com/product.png</pic>\n</detail>"
    )
    state = SimpleNamespace(
        status="completed",
        response=AgentResponse(message=final_text),
        run_id="assistant-run",
        trace_id="1234567890abcdef1234567890abcdef",
        user_id="user-1",
        session_id="session-1",
    )

    def run_request(request, *, event_sink, **kwargs):
        event_sink.emit(
            AgentEvent(
                type="tool_finished",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name="shopping_search",
                payload={"post_tool_call": {"status": "succeeded"}},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="response_delta",
                session_id=state.session_id,
                run_id=state.run_id,
                text=final_text,
            )
        )
        return SimpleNamespace(
            state=state,
            runtime=SimpleNamespace(trace_store=trace_store),
        )

    events = []

    async def collect_event(event):
        events.append(event)

    result = await GatewayRuntimeAdapter(
        run_request=run_request,
        load_env=False,
    ).run_turn(
        RealtimeAgentRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            run_id="gateway-run",
            text="找草莓牛奶",
        ),
        event_sink=collect_event,
    )

    assert result.status == "completed"
    assert result.response_text == final_text
    assert result.metadata["response_delivery_source"] == "assistant_response"
    assert [event.text for event in events if event.type == "response.chunk"] == [
        final_text
    ]
    delivered = next(
        event
        for event in trace_store.list_by_trace(state.trace_id)
        if event.canonical_event == "response.delivered"
    )
    assert delivered.attributes["source"] == "assistant_response"
    assert delivered.attributes["message_chars"] == len(final_text)
