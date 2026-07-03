import asyncio
import time
from types import SimpleNamespace

from assistant_agent.agent.state import AgentState
from assistant_agent.agent_routing import WORKER_AGENT_ID
from assistant_agent.realtime import (
    AgentGraphRealtimeBackend,
    ProgressPolicy,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
)
from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentInstance,
    AgentMessage,
    AgentSessionRef,
)
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.agent_communication import AgentCommunicationService
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance
from assistant_agent.services.agent_transports import LocalAgentTransport


class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    async def cancelled(self) -> None:
        return None

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


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


def test_agent_graph_realtime_backend_forwards_runtime_progress_events() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        event_sink = kwargs["event_sink"]
        event_sink.emit(
            AgentEvent(
                type="task_started",
                session_id=request.session_id,
                run_id="assistant-run-1",
                payload={"user_id": request.user_id},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="tool_progress",
                session_id=request.session_id,
                run_id="assistant-run-1",
                tool_name="product_search",
                progress=0.5,
            )
        )
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    assert [event.type for event in events[:2]] == ["run.progress", "run.progress"]
    assert events[0].payload["status"] == "started"
    assert events[1].payload["tool_name"] == "product_search"
    assert events[1].payload["progress"] == 0.5


def test_agent_graph_realtime_backend_emits_idle_heartbeat_progress() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        kwargs["event_sink"].emit(
            AgentEvent(
                type="task_started",
                session_id=request.session_id,
                run_id="assistant-run-1",
                payload={"user_id": request.user_id},
            )
        )
        time.sleep(0.16)
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(
        run_request=fake_run_assistant_request,
        progress_policy=ProgressPolicy(
            min_interval_s=0.0,
            heartbeat_interval_s=0.05,
        ),
    )
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    heartbeats = [
        event for event in events if event.type == "run.progress" and event.payload.get("heartbeat")
    ]
    assert result.status == "completed"
    assert heartbeats
    assert heartbeats[0].text == "Still processing the request."
    assert heartbeats[0].payload["elapsed_since_update_s"] >= 0.05


def test_agent_graph_realtime_backend_keeps_delegation_inside_main_runtime() -> None:
    class WorkerRuntime:
        def __init__(self) -> None:
            self.requests: list[UserRequest] = []

        def run_state(self, request: UserRequest) -> AgentState:
            self.requests.append(request)
            state = AgentState.from_request(request, run_id="worker-run-1")
            state.set_response(
                AgentResponse(
                    message=f"worker handled: {request.text}",
                    data={"agent_id": WORKER_AGENT_ID},
                )
            )
            return state

    worker_runtime = WorkerRuntime()
    directory = AgentDirectory(
        [
            default_agent_instance(can_delegate=True, allowed_targets=[WORKER_AGENT_ID]),
            AgentInstance(
                agent_id=WORKER_AGENT_ID,
                display_name="Worker Agent",
                capabilities=["chat", "tool_calling"],
                transports=["local"],
            ),
        ]
    )
    service = AgentCommunicationService(
        directory=directory,
        transports=[LocalAgentTransport({WORKER_AGENT_ID: worker_runtime})],
    )
    controller_requests: list[UserRequest] = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        controller_requests.append(request)
        delegated = service.send_message(
            target_agent_id=WORKER_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(
                user_id=request.user_id,
                session_id=request.session_id,
                parent_run_id="controller-run-1",
                parent_trace_id="controller-trace-1",
            ),
            message=AgentMessage(
                text=f"delegate: {request.text}",
                metadata=dict(request.metadata),
            ),
        )
        state = AgentState.from_request(request, run_id="controller-run-1")
        state.tool_results.append(
            ToolResult(
                tool_name="delegate_to_agent",
                success=delegated.status == "completed",
                data=delegated.model_dump(mode="json"),
            )
        )
        worker_text = delegated.artifacts[0].text if delegated.artifacts else ""
        state.set_response(
            AgentResponse(
                message=f"controller delegated: {worker_text}",
                data={"delegated_status": delegated.status},
            )
        )
        return SimpleNamespace(state=state)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="gateway-run-1",
                turn_id="turn-1",
                text="coordinate realtime work",
                metadata={
                    "source": "realtime_media_websocket",
                    "gateway": {"frame_type": "call.incoming", "session_config": {"call_id": "call-1"}},
                    "realtime": {"call_id": "call-1"},
                },
            )
        )
    )

    assert result.status == "completed"
    assert result.run_id == "gateway-run-1"
    assert result.metadata["assistant_run_id"] == "controller-run-1"
    assert len(controller_requests) == 1
    assert len(worker_runtime.requests) == 1
    assert controller_requests[0].metadata["gateway"]["frame_type"] == "call.incoming"
    assert controller_requests[0].metadata["realtime"]["call_id"] == "call-1"
    worker_request = worker_runtime.requests[0]
    assert worker_request.text == "delegate: coordinate realtime work"
    assert worker_request.metadata["source"] == "realtime_media_websocket"
    assert "agent_communication" in worker_request.metadata
    assert "agent_context" in worker_request.metadata
    assert "gateway" not in worker_request.metadata
    assert "realtime" not in worker_request.metadata
    metadata_text = repr(worker_request.metadata)
    assert "call.incoming" not in metadata_text
    assert "call.hangup" not in metadata_text


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


def test_agent_graph_realtime_backend_post_run_cancel_includes_token_metadata() -> None:
    token = MutableCancelToken(
        metadata={
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
            "deadline_ms": 50,
        }
    )

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        token.cancelled = True
        return _completed_artifacts(request, run_id="assistant-run-1", trace_id="trace-1")

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            cancel_token=token,
        )
    )

    assert result.status == "cancelled"
    assert result.metadata == {
        "assistant_run_id": "assistant-run-1",
        "cancel_source": "deadline",
        "cancel_reason": "run_deadline_expired",
        "deadline_ms": 50,
        "cancel_phase": "post_run",
        "best_effort": True,
    }


def test_agent_graph_realtime_backend_maps_internal_agent_cancel_without_final_events() -> None:
    token = MutableCancelToken()
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        assert kwargs["cancel_token"] is token
        state = AgentState.from_request(request, run_id="assistant-run-1")
        state.trace_id = "trace-1"
        state.cancel(
            details={
                "cancel_phase": "after_node",
                "node_name": "assistant_decision",
            }
        )
        return SimpleNamespace(state=state)

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
            cancel_token=token,
        )
    )

    assert result.status == "cancelled"
    assert result.run_id == "runtime-run-1"
    assert result.trace_id == "trace-1"
    assert result.metadata == {
        "assistant_run_id": "assistant-run-1",
        "cancel_phase": "after_node",
        "best_effort": True,
    }
    assert [event.type for event in events] == []


def test_agent_graph_realtime_backend_maps_internal_agent_cancel_metadata() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        state = AgentState.from_request(request, run_id="assistant-run-1")
        state.trace_id = "trace-1"
        state.cancel(
            details={
                "cancel_phase": "after_node",
                "cancel_source": "deadline",
                "cancel_reason": "run_deadline_expired",
                "deadline_ms": 75,
            }
        )
        return SimpleNamespace(state=state)

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

    assert result.status == "cancelled"
    assert result.metadata == {
        "assistant_run_id": "assistant-run-1",
        "cancel_source": "deadline",
        "cancel_reason": "run_deadline_expired",
        "deadline_ms": 75,
        "cancel_phase": "after_node",
        "best_effort": True,
    }


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


def test_agent_graph_realtime_backend_does_not_duplicate_streamed_response_delta() -> None:
    events = []

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        kwargs["event_sink"].emit(
            AgentEvent(
                type="response_delta",
                session_id=request.session_id,
                run_id="assistant-run-1",
                text="Alpha ",
                payload={"token_streaming": True, "source": "direct_chat"},
            )
        )
        kwargs["event_sink"].emit(
            AgentEvent(
                type="response_delta",
                session_id=request.session_id,
                run_id="assistant-run-1",
                text="beta.",
                payload={"token_streaming": True, "source": "direct_chat"},
            )
        )
        return _completed_artifacts(
            request,
            run_id="assistant-run-1",
            trace_id="trace-1",
            message="Alpha beta.",
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
    assert [event.type for event in events] == ["response.chunk", "response.chunk", "response.final"]
    assert [event.text for event in events] == ["Alpha ", "beta.", "Alpha beta."]
    assert events[0].payload["agent_event_type"] == "response_delta"


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
        "run.progress",
        "run.progress",
        "tool.started",
        "run.progress",
        "tool.finished",
        "run.progress",
        "tool.failed",
        "trace.decision",
        "trace.observation",
        "error",
        "response.chunk",
        "response.final",
    ]
    progress_events = [event for event in events if event.type == "run.progress"]
    assert [event.payload["status"] for event in progress_events] == [
        "working",
        "working",
        "completed",
        "failed",
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
