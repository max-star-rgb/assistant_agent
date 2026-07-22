"""Regression for entry-layer response delivery consistency."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from assistant_agent.realtime.agent_graph_backend import (
    AgentGraphRealtimeBackend,
    _successful_shopping_detail_result,
)
from assistant_agent.realtime.types import RealtimeAgentRequest
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.trace_store import InMemoryTraceStore


def _shopping_result() -> ToolResult:
    offer = {
        "offer_id": "offer-1",
        "product_id": "product-1",
        "title": "草莓牛奶",
        "platform": "taobao",
        "price": 12.0,
        "total_price": 12.0,
        "product_url": "https://example.com/product",
        "image_url": "https://example.com/product.png",
    }
    return ToolResult(
        tool_name="shopping_search",
        success=True,
        output_ref="mock://shopping/milk",
        data={
            "query": "草莓牛奶",
            "search": {
                "items": [],
                "provider": "mock",
                "total": 1,
                "output_ref": "mock://shopping/milk",
            },
            "items": [],
            "offers": [offer],
            "summary": "已找到商品。",
            "provider": "mock",
            "output_ref": "mock://shopping/milk",
        },
    )


def test_shopping_presenter_is_the_realtime_result_and_delivered_trace() -> None:
    asyncio.run(_assert_shopping_presenter_is_the_realtime_result_and_delivered_trace())


def test_shopping_search_alone_does_not_authorize_detail_presentation() -> None:
    state = SimpleNamespace(tool_results=[_shopping_result()])

    assert _successful_shopping_detail_result(state) is None


async def _assert_shopping_presenter_is_the_realtime_result_and_delivered_trace() -> None:
    trace_store = InMemoryTraceStore()
    state = SimpleNamespace(
        status="completed",
        response=AgentResponse(message="模型认为结果不相关。"),
        run_id="assistant-run",
        trace_id="1234567890abcdef1234567890abcdef",
        user_id="user-1",
        session_id="session-1",
        tool_results=[_shopping_result()],
    )
    state.tool_results.append(
        ToolResult(
            tool_name="shopping_detail_present",
            success=True,
            data={
                "output_ref": "mock://shopping/milk",
                "summary": "已选择商品结果作为本轮最终购物展示。",
            },
        )
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
                type="tool_finished",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name="shopping_detail_present",
                payload={"post_tool_call": {"status": "succeeded"}},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="response_delta",
                session_id=state.session_id,
                run_id=state.run_id,
                text=state.response.message,
            )
        )
        return SimpleNamespace(
            state=state,
            runtime=SimpleNamespace(trace_store=trace_store),
        )

    events = []

    async def collect_event(event):
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request=run_request, load_env=False)
    result = await backend.run_turn(
        RealtimeAgentRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            run_id="gateway-run",
            text="找草莓牛奶",
            metadata={
                "source": "gateway_websocket",
                "transport": "websocket",
                "request_identity": {},
                "gateway": {
                    "entry_capabilities": {"supports_shopping_detail_v1": True}
                },
            },
        ),
        event_sink=collect_event,
    )

    assert result.status == "completed"
    assert result.response_text.startswith("已找到商品。\n<detail>")
    assert "模型认为结果不相关" not in result.response_text
    assert result.metadata["response_delivery_source"] == "shopping_detail_v1"
    assert [event.type for event in events if event.type.startswith("response.")] == [
        "response.chunk",
        "response.final",
    ]
    delivered = next(
        event
        for event in trace_store.list_by_trace(state.trace_id)
        if event.canonical_event == "response.delivered"
    )
    assert delivered.attributes["source"] == "shopping_detail_v1"
    assert delivered.attributes["message_chars"] == len(result.response_text)
