from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.gateway.runtime_types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
)
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.models import ToolResult


def _tool_result() -> ToolResult:
    return ToolResult(
        tool_name="shopping_search",
        success=True,
        data={
            "outcome": "success",
            "total_cost": 2599.0,
            "within_budget": True,
            "needs": [],
            "selections": [
                {
                    "keyword": "小米14",
                    "quantity": 1,
                    "unit_price": 2599.0,
                    "subtotal": 2599.0,
                    "product": {
                        "product_id": "p1",
                        "title": "小米14 12+256GB",
                        "price": 2599.0,
                        "platform": "jd",
                        "shop": "京东",
                        "product_url": "https://u.jd.com/one",
                        "image_url": "https://img.example/one.jpg",
                    },
                }
            ],
            "summary": "找到一个候选。",
            "provider": "offline",
        },
    )


def test_gateway_appends_detail_without_mutating_runtime_response() -> None:
    captured_state: list[AgentState] = []

    def run_request(request: Any, **kwargs: Any) -> Any:
        state = AgentState.from_request(request)
        state.tool_results.append(_tool_result())
        state.set_response(AgentResponse(message="这款符合你的预算。"))
        captured_state.append(state)
        return SimpleNamespace(
            runtime=SimpleNamespace(trace_store=None),
            state=state,
            events=[],
        )

    realtime_events: list[RealtimeAgentEvent] = []

    async def run() -> Any:
        async def collect(event: RealtimeAgentEvent) -> None:
            realtime_events.append(event)

        return await GatewayRuntimeAdapter(
            run_request=run_request,
            load_env=False,
            enable_conversation_history=False,
        ).run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                text="推荐小米14",
                metadata={
                    "gateway": {
                        "entry_capabilities": {
                            "supports_shopping_detail_v1": True
                        }
                    }
                },
            ),
            event_sink=collect,
        )

    result = asyncio.run(run())

    assert captured_state[0].response is not None
    assert captured_state[0].response.message == "这款符合你的预算。"
    assert result.response_text.startswith("这款符合你的预算。\n<detail>\n")
    assert result.metadata["response_delivery_source"] == "shopping_detail_v1"
    assert realtime_events[-1].type == "response.final"
    assert realtime_events[-1].text == result.response_text


def test_streaming_delivery_emits_detail_as_non_token_supplement() -> None:
    def run_request(request: Any, **kwargs: Any) -> Any:
        event_sink = kwargs["event_sink"]
        state = AgentState.from_request(request)
        event_sink.emit(
            AgentEvent(
                type="response_delta",
                session_id=state.session_id,
                run_id=state.run_id,
                text="这款符合你的预算。",
                payload={"realtime": {"token_streaming": True}},
            )
        )
        state.tool_results.append(_tool_result())
        state.set_response(AgentResponse(message="这款符合你的预算。"))
        return SimpleNamespace(
            runtime=SimpleNamespace(trace_store=None),
            state=state,
            events=[],
        )

    realtime_events: list[RealtimeAgentEvent] = []

    async def run() -> Any:
        async def collect(event: RealtimeAgentEvent) -> None:
            realtime_events.append(event)

        return await GatewayRuntimeAdapter(
            run_request=run_request,
            load_env=False,
            enable_conversation_history=False,
        ).run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                text="推荐小米14",
                metadata={
                    "gateway": {
                        "entry_capabilities": {
                            "supports_shopping_detail_v1": True
                        }
                    }
                },
            ),
            event_sink=collect,
        )

    result = asyncio.run(run())
    detail_chunks = [
        event
        for event in realtime_events
        if event.type == "response.chunk" and event.content_type == "detail"
    ]

    assert len(detail_chunks) == 1
    assert detail_chunks[0].payload["token_streaming"] is False
    assert detail_chunks[0].text == "\n" + result.response_text.split("\n", 1)[1]
