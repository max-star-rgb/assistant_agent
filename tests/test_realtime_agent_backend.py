import asyncio
from types import SimpleNamespace

from assistant_agent.agent.state import AgentState
from assistant_agent.realtime import AgentGraphRealtimeBackend, RealtimeAgentRequest
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest


class MutableCancelToken:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled

    async def cancelled(self) -> None:
        return None


def _completed_artifacts(
    request: UserRequest,
    *,
    run_id: str = "assistant-run-1",
    trace_id: str = "trace-1",
    message: str = "Alpha beta gamma.",
    output_refs: list[str] | None = None,
    followup_question: str | None = None,
) -> SimpleNamespace:
    state = AgentState.from_request(request, run_id=run_id)
    state.trace_id = trace_id
    state.set_response(
        AgentResponse(
            message=message,
            output_refs=list(output_refs or []),
            followup_question=followup_question,
        )
    )
    return SimpleNamespace(state=state)


def test_agent_graph_realtime_backend_maps_request_metadata_and_fields() -> None:
    captured: dict[str, object] = {}

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    realtime_request = RealtimeAgentRequest(
        user_id="user-1",
        session_id="session-1",
        run_id="runtime-run-1",
        turn_id="turn-1",
        text="hello",
        image_ids=["image-1"],
        video_ids=["video-1"],
        audio_id="audio-1",
        metadata={"channel": "phone", "realtime": {"call_id": "call-1"}},
    )

    result = asyncio.run(backend.run_turn(realtime_request))

    request = captured["request"]
    assert isinstance(request, UserRequest)
    assert request.user_id == "user-1"
    assert request.session_id == "session-1"
    assert request.text == "hello"
    assert request.image_ids == ["image-1"]
    assert request.video_ids == ["video-1"]
    assert request.audio_id == "audio-1"
    assert request.metadata["channel"] == "phone"
    assert request.metadata["source"] == "realtime_agent_backend"
    assert request.metadata["realtime"] == {
        "call_id": "call-1",
        "run_id": "runtime-run-1",
        "turn_id": "turn-1",
    }
    assert captured["kwargs"]["load_env"] is True
    assert captured["kwargs"]["enable_conversation_history"] is True
    assert result.status == "completed"


def test_agent_graph_realtime_backend_preserves_existing_metadata_source() -> None:
    captured: dict[str, UserRequest] = {}

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        captured["request"] = request
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)

    asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                text="hello",
                metadata={"source": "phone_runtime"},
            )
        )
    )

    assert captured["request"].metadata["source"] == "phone_runtime"


def test_agent_graph_realtime_backend_pre_run_cancel_does_not_call_runner() -> None:
    calls = 0

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="runtime-run-1",
                text="hello",
            ),
            cancel_token=MutableCancelToken(cancelled=True),
        )
    )

    assert calls == 0
    assert result.status == "cancelled"
    assert result.run_id == "runtime-run-1"
    assert result.metadata == {"cancel_phase": "pre_run", "best_effort": True}


def test_agent_graph_realtime_backend_post_run_cancel_skips_final_events() -> None:
    token = MutableCancelToken()
    calls = 0
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        token.cancelled = True
        return _completed_artifacts(request, run_id="assistant-run-1", trace_id="trace-1")

    async def collect(event) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
            cancel_token=token,
        )
    )

    assert calls == 1
    assert result.status == "cancelled"
    assert result.run_id == "assistant-run-1"
    assert result.trace_id == "trace-1"
    assert result.metadata == {
        "assistant_run_id": "assistant-run-1",
        "cancel_phase": "post_run",
        "best_effort": True,
    }
    assert [event.type for event in events] == []


def test_agent_graph_realtime_backend_completed_run_sends_chunk_then_final() -> None:
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        kwargs["event_sink"].emit(
            AgentEvent(
                type="final_response",
                session_id=request.session_id,
                run_id="ignored-run-final",
                text="ignored runtime final",
            )
        )
        return _completed_artifacts(
            request,
            run_id="assistant-run-1",
            trace_id="trace-1",
            message="Alpha beta gamma.",
        )

    async def collect(event) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    assert [event.type for event in events] == ["response.chunk", "response.final"]
    assert [event.text for event in events] == ["Alpha beta gamma.", "Alpha beta gamma."]


def test_agent_graph_realtime_backend_result_fields_use_external_and_internal_run_ids() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        return _completed_artifacts(
            request,
            run_id="assistant-run-1",
            trace_id="trace-1",
            message="Done.",
            output_refs=["mock://artifact"],
            followup_question="Anything else?",
        )

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="runtime-run-1",
                text="hello",
            )
        )
    )

    assert result.response_text == "Done."
    assert result.trace_id == "trace-1"
    assert result.run_id == "runtime-run-1"
    assert result.output_refs == ["mock://artifact"]
    assert result.expects_reply is True
    assert result.metadata["assistant_run_id"] == "assistant-run-1"


def test_agent_graph_realtime_backend_uses_assistant_run_id_when_external_run_id_missing() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        return _completed_artifacts(request, run_id="assistant-run-1")

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello")
        )
    )

    assert result.run_id == "assistant-run-1"
    assert result.metadata["assistant_run_id"] == "assistant-run-1"


def test_agent_graph_realtime_backend_forwards_runtime_tool_trace_and_error_events() -> None:
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        sink = kwargs["event_sink"]
        sink.emit(
            AgentEvent(type="graph_node_started", session_id=request.session_id, run_id="run-1")
        )
        sink.emit(
            AgentEvent(
                type="tool_started",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
            )
        )
        sink.emit(
            AgentEvent(
                type="tool_finished",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                output_ref="mock://result",
            )
        )
        sink.emit(
            AgentEvent(
                type="tool_failed",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="price_compare",
                error={"code": "TOOL_FAILED", "message": "tool failed"},
            )
        )
        sink.emit(
            AgentEvent(
                type="agent_trace_decision",
                session_id=request.session_id,
                run_id="run-1",
                payload={"decision_trace": {"event": "decision"}},
            )
        )
        sink.emit(
            AgentEvent(
                type="agent_trace_observation",
                session_id=request.session_id,
                run_id="run-1",
                payload={"decision_trace": {"event": "observation"}},
            )
        )
        sink.emit(
            AgentEvent(
                type="task_failed",
                session_id=request.session_id,
                run_id="run-1",
                error={"code": "TASK_FAILED", "message": "task failed"},
            )
        )
        return _completed_artifacts(request, run_id="assistant-run-1", message="Done.")

    async def collect(event) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    assert [event.type for event in events] == [
        "tool.started",
        "tool.finished",
        "tool.failed",
        "trace.decision",
        "trace.observation",
        "error",
        "response.chunk",
        "response.final",
    ]


def test_agent_graph_realtime_backend_exception_returns_error_and_emits_error_event() -> None:
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        raise RuntimeError("backend exploded")

    async def collect(event) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="runtime-run-1",
                text="hello",
            ),
            event_sink=collect,
        )
    )

    assert result.status == "error"
    assert result.run_id == "runtime-run-1"
    assert result.metadata == {
        "error_type": "RuntimeError",
        "error_message": "backend exploded",
    }
    assert [event.type for event in events] == ["error"]
    assert events[0].text == "backend exploded"
    assert events[0].payload["error_type"] == "RuntimeError"
