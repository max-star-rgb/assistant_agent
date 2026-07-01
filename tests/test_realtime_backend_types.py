import asyncio

from assistant_agent.realtime import (
    RealtimeAgentBackend,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
    RealtimeBackendCapabilities,
)


def test_realtime_capabilities_defaults_match_plan() -> None:
    capabilities = RealtimeBackendCapabilities()

    assert capabilities.supports_token_streaming is False
    assert capabilities.supports_tool_event_streaming is True
    assert capabilities.supports_best_effort_cancel is True
    assert capabilities.supports_hard_cancel is False
    assert capabilities.supports_multimodal_refs is True


def test_realtime_request_event_and_result_are_serializable() -> None:
    request = RealtimeAgentRequest(
        user_id="user-1",
        session_id="session-1",
        run_id="run-1",
        turn_id="turn-1",
        text="hello",
        metadata={"source": "test"},
    )
    event = RealtimeAgentEvent(type="response.chunk", text="hello")
    result = RealtimeAgentResult(
        status="completed",
        response_text="hello",
        run_id=request.run_id,
        trace_id="trace-1",
        output_refs=["mock://result"],
    )

    assert request.model_dump(mode="json")["metadata"] == {"source": "test"}
    assert event.model_dump(mode="json")["content_type"] == "text"
    assert result.model_dump(mode="json")["status"] == "completed"


def test_realtime_backend_protocol_shape() -> None:
    class DummyBackend:
        @property
        def capabilities(self) -> RealtimeBackendCapabilities:
            return RealtimeBackendCapabilities()

        async def run_turn(
            self,
            request: RealtimeAgentRequest,
            *,
            event_sink=None,
            cancel_token=None,
        ) -> RealtimeAgentResult:
            if event_sink is not None:
                await event_sink(RealtimeAgentEvent(type="response.chunk", text=request.text))
            return RealtimeAgentResult(status="completed", response_text=request.text)

    backend: RealtimeAgentBackend = DummyBackend()
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    assert result.response_text == "hello"
    assert [event.type for event in events] == ["response.chunk"]
