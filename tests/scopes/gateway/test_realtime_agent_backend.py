import asyncio
import time
from types import SimpleNamespace

from assistant_agent.agent.event_stream import AgentRunStream
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.agent_routing import WORKER_AGENT_ID
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.realtime import (
    AgentGraphRealtimeBackend,
    ProgressPolicy,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
)
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentInstance,
    AgentMessage,
    AgentSessionRef,
)
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
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


def test_app_shopping_detail_suppresses_llm_delta_and_emits_one_presented_chunk() -> None:
    def fake_stream(request: UserRequest, **kwargs) -> AgentRunStream[SimpleNamespace]:
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[SimpleNamespace] = AgentRunStream(loop=loop)

        async def publish() -> None:
            stream.emit(AgentEvent(type="response_delta", session_id=request.session_id, text="先说一点"))
            stream.emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=request.session_id,
                    tool_name="price_compare",
                    payload={"post_tool_call": {"status": "succeeded"}},
                )
            )
            stream.emit(AgentEvent(type="response_delta", session_id=request.session_id, text="伪造<detail>"))
            artifacts = _completed_artifacts(request, message="自然语言摘要")
            offer = {
                "offer_id": "jd:1",
                "product_id": "jd:1",
                "title": "手机",
                "platform": "jd",
                "price": 99,
                "total_price": 99,
                "product_url": "https://item.jd.com/1.html",
                "image_url": "https://img.example/1.jpg",
                "url_status": "verified",
            }
            artifacts.state.tool_results.append(
                ToolResult(
                    tool_name="price_compare",
                    success=True,
                    data={"query": "手机", "summary": "自然语言摘要", "offers": [offer], "best_offer": offer},
                )
            )
            later_ineligible_offer = {**offer, "offer_id": "jd:2", "product_id": "jd:2", "image_url": None}
            artifacts.state.tool_results.append(
                ToolResult(
                    tool_name="price_compare",
                    success=True,
                    data={
                        "query": "手机",
                        "summary": "后续无卡片比价结果",
                        "offers": [later_ineligible_offer],
                        "best_offer": later_ineligible_offer,
                    },
                )
            )
            stream.set_result(artifacts)

        asyncio.create_task(publish())
        return stream

    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request_stream=fake_stream)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="u",
                session_id="s",
                text="比价",
                metadata={
                    "source": "gateway_websocket",
                    "transport": "websocket",
                    "request_identity": {"user_id": "u"},
                    "gateway": {"entry_capabilities": {"supports_shopping_detail_v1": True}},
                },
            ),
            event_sink=collect,
        )
    )

    chunks = [event.text for event in events if event.type == "response.chunk"]
    assert chunks == [
        "自然语言摘要\n<detail>\n1. 京东 - 手机 99元 <link>https://item.jd.com/1.html</link> "
        "<pic>https://img.example/1.jpg</pic>\n</detail>"
    ]
    assert result.response_text == "自然语言摘要"


def test_app_shopping_detail_accepts_unified_shopping_search_result() -> None:
    def fake_stream(request: UserRequest, **kwargs) -> AgentRunStream[SimpleNamespace]:
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[SimpleNamespace] = AgentRunStream(loop=loop)

        async def publish() -> None:
            stream.emit(AgentEvent(type="response_delta", session_id=request.session_id, text="先说一点"))
            stream.emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=request.session_id,
                    tool_name="shopping_search",
                    payload={"post_tool_call": {"status": "succeeded"}},
                )
            )
            stream.emit(AgentEvent(type="response_delta", session_id=request.session_id, text="不要展示这段"))
            artifacts = _completed_artifacts(request, message="推荐这款蓝牙耳机。")
            offer = {
                "offer_id": "taobao:1",
                "product_id": "taobao:1",
                "title": "蓝牙耳机",
                "platform": "taobao",
                "price": 29.9,
                "total_price": 29.9,
                "product_url": "https://item.taobao.com/item.htm?id=1",
                "image_url": "https://img.alicdn.com/1.jpg",
                "url_status": "unverified",
            }
            artifacts.state.tool_results.append(
                ToolResult(
                    tool_name="shopping_search",
                    success=True,
                    data={
                        "query": "蓝牙耳机",
                        "search": {
                            "items": [],
                            "provider": "haodanku",
                            "query_used": "蓝牙耳机",
                            "total": 1,
                        },
                        "comparison": {
                            "query": "蓝牙耳机",
                            "summary": "推荐这款蓝牙耳机。",
                            "offers": [offer],
                            "best_offer": offer,
                            "provider": "haodanku",
                        },
                        "items": [],
                        "offers": [offer],
                        "best_offer": offer,
                        "summary": "推荐这款蓝牙耳机。",
                        "provider": "haodanku",
                    },
                )
            )
            stream.set_result(artifacts)

        asyncio.create_task(publish())
        return stream

    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request_stream=fake_stream)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="u",
                session_id="s",
                text="我想买蓝牙耳机",
                metadata={
                    "source": "gateway_websocket",
                    "transport": "websocket",
                    "request_identity": {"user_id": "u"},
                    "gateway": {"entry_capabilities": {"supports_shopping_detail_v1": True}},
                },
            ),
            event_sink=collect,
        )
    )

    chunks = [event.text for event in events if event.type == "response.chunk"]
    assert chunks == [
        "推荐这款蓝牙耳机。\n<detail>\n1. 淘宝 - 蓝牙耳机 29.9元 "
        "<link>https://item.taobao.com/item.htm?id=1</link> "
        "<pic>https://img.alicdn.com/1.jpg</pic>\n</detail>"
    ]
    assert result.response_text == "推荐这款蓝牙耳机。"


def test_agent_service_shopping_detail_accepts_unified_shopping_search_result() -> None:
    def fake_stream(request: UserRequest, **kwargs) -> AgentRunStream[SimpleNamespace]:
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[SimpleNamespace] = AgentRunStream(loop=loop)

        async def publish() -> None:
            stream.emit(AgentEvent(type="response_delta", session_id=request.session_id, text="不要展示这段"))
            stream.emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=request.session_id,
                    tool_name="shopping_search",
                    payload={"post_tool_call": {"status": "succeeded"}},
                )
            )
            artifacts = _completed_artifacts(request, message="推荐这款蓝牙耳机。")
            offer = {
                "offer_id": "taobao:1",
                "product_id": "taobao:1",
                "title": "蓝牙耳机",
                "platform": "taobao",
                "price": 29.9,
                "total_price": 29.9,
                "product_url": "https://item.taobao.com/item.htm?id=1",
                "image_url": "https://img.alicdn.com/1.jpg",
                "url_status": "unverified",
            }
            artifacts.state.tool_results.append(
                ToolResult(
                    tool_name="shopping_search",
                    success=True,
                    data={
                        "query": "蓝牙耳机",
                        "search": {
                            "items": [],
                            "provider": "haodanku",
                            "query_used": "蓝牙耳机",
                            "total": 1,
                        },
                        "comparison": {
                            "query": "蓝牙耳机",
                            "summary": "推荐这款蓝牙耳机。",
                            "offers": [offer],
                            "best_offer": offer,
                            "provider": "haodanku",
                        },
                        "items": [],
                        "offers": [offer],
                        "best_offer": offer,
                        "summary": "推荐这款蓝牙耳机。",
                        "provider": "haodanku",
                    },
                )
            )
            stream.set_result(artifacts)

        asyncio.create_task(publish())
        return stream

    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    backend = AgentGraphRealtimeBackend(run_request_stream=fake_stream)
    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="10086",
                session_id="agent-service-session",
                text="我想买蓝牙耳机",
                metadata={
                    "transport": "agent_service_websocket",
                    "gateway": {
                        "entry_capabilities": {"supports_shopping_detail_v1": True},
                        "session_config": {"entry_profile": "agent_service"},
                    },
                },
            ),
            event_sink=collect,
        )
    )

    chunks = [event.text for event in events if event.type == "response.chunk"]
    assert chunks == [
        "推荐这款蓝牙耳机。\n<detail>\n1. 淘宝 - 蓝牙耳机 29.9元 "
        "<link>https://item.taobao.com/item.htm?id=1</link> "
        "<pic>https://img.alicdn.com/1.jpg</pic>\n</detail>"
    ]
    assert result.response_text == "推荐这款蓝牙耳机。"


def test_untrusted_shopping_capability_does_not_change_streaming() -> None:
    module = __import__(
        "assistant_agent.realtime.agent_graph_backend", fromlist=["shopping_detail_enabled"]
    )
    assert not module.shopping_detail_enabled(
        {"gateway": {"entry_capabilities": {"supports_shopping_detail_v1": True}}}
    )
    assert not module.shopping_detail_enabled(
        {
            "transport": "agent_service_websocket",
            "gateway": {"entry_capabilities": {"supports_shopping_detail_v1": True}},
        }
    )


def _no_visible_realtime_progress() -> dict[str, object]:
    return {
        "first_visible_event_ms": None,
        "sla_fallback_emitted": False,
        "user_visible_event_count": 0,
    }


def _assert_realtime_cancel_contract(
    metadata: dict[str, object],
    *,
    cancelled_by: str,
    phase: str,
) -> None:
    assert metadata["stale_outputs"] is True
    assert metadata["can_reuse_tool_result"] is False
    assert metadata["speakable"] is False
    assert metadata["realtime_turn_cancellation"] == {
        "cancelled_by": cancelled_by,
        "phase": phase,
        "stale_outputs": True,
        "can_reuse_tool_result": False,
        "speakable": False,
    }


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


def test_agent_graph_realtime_backend_preserves_execution_strategy_metadata() -> None:
    captured: dict[str, object] = {}

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        captured["request"] = request
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    realtime_request = RealtimeAgentRequest(
        user_id="user-1",
        session_id="session-1",
        text="plan this",
        metadata={"execution_strategy": "plan_and_solve"},
    )

    result = asyncio.run(backend.run_turn(realtime_request))

    request = captured["request"]
    assert isinstance(request, UserRequest)
    assert request.execution_strategy == "plan_and_solve"
    assert result.status == "completed"


def test_agent_graph_realtime_backend_consumes_run_request_stream_provider() -> None:
    captured: dict[str, object] = {}

    def fake_run_assistant_request_stream(
        request: UserRequest,
        **kwargs,
    ) -> AgentRunStream[SimpleNamespace]:
        captured["request"] = request
        captured["kwargs"] = kwargs
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[SimpleNamespace] = AgentRunStream(loop=loop)

        async def publish() -> None:
            stream.emit(
                AgentEvent(
                    type="response_delta",
                    session_id=request.session_id,
                    run_id="assistant-run-1",
                    text="Alpha ",
                    payload={"token_streaming": True, "source": "stream_provider"},
                )
            )
            stream.set_result(
                _completed_artifacts(request, run_id="assistant-run-1", message="Alpha beta.")
            )

        asyncio.create_task(publish())
        return stream

    backend = AgentGraphRealtimeBackend(run_request_stream=fake_run_assistant_request_stream)
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
    assert captured["request"].metadata["source"] == "realtime_agent_backend"
    assert captured["kwargs"]["load_env"] is True
    assert captured["kwargs"]["enable_conversation_history"] is True
    assert "event_sink" not in captured["kwargs"]
    assert [event.type for event in events] == ["response.chunk", "response.final"]
    assert [event.text for event in events] == ["Alpha ", "Alpha beta."]


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


def test_agent_graph_realtime_backend_forwards_replaceable_progress_message() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        event_sink = kwargs["event_sink"]
        event_sink.emit(
            AgentEvent(
                type="progress_message",
                session_id=request.session_id,
                run_id="assistant-run-1",
                tool_name="image_generation",
                text="我开始生成，可能需要一点时间。",
                payload={
                    "source": "native_tool_wait",
                    "replaceable": True,
                    "tool_name": "image_generation",
                },
            )
        )
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="生成一个蛋糕"),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    progress = next(event for event in events if event.type == "run.progress")
    assert progress.text == "我开始生成，可能需要一点时间。"
    assert progress.payload["agent_event_type"] == "progress_message"
    assert progress.payload["replaceable"] is True
    assert progress.payload["tool_name"] == "image_generation"


def test_agent_graph_realtime_backend_streams_read_only_native_tool_path() -> None:
    class ScriptedNativeToolAdapter:
        provider = "scripted-native"

        def __init__(self) -> None:
            self.requests: list[ChatRequest] = []

        def chat(self, request: ChatRequest) -> ChatResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ChatResult(
                    response_text="",
                    tool_calls=[
                        NativeToolCall(
                            id="call_1",
                            name="web_search",
                            arguments={"query": "OpenAI latest news", "limit": 2},
                            raw={
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "web_search", "arguments": "{}"},
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    message_kind="tool_call",
                    provider="scripted-native",
                    model="native-test",
                )
            return ChatResult(
                response_text="Realtime answer from web_search observation.",
                finish_reason="stop",
                message_kind="final_answer",
                provider="scripted-native",
                model="native-test",
            )

    adapter = ScriptedNativeToolAdapter()
    captured: dict[str, AgentState] = {}

    def run_with_scripted_runtime(request: UserRequest, **kwargs) -> SimpleNamespace:
        runtime = AgentGraphRuntime(
            config=ProviderConfig(),
            memory_store=InMemoryStore(),
            chat_adapter=adapter,
        )
        state = runtime.run_state(
            request,
            event_sink=kwargs.get("event_sink"),
            cancel_token=kwargs.get("cancel_token"),
        )
        captured["state"] = state
        return SimpleNamespace(state=state)

    backend = AgentGraphRealtimeBackend(
        run_request=run_with_scripted_runtime,
        progress_policy=ProgressPolicy(first_progress_timeout_s=0),
    )
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                text="联网搜索 OpenAI 最近发布了什么",
            ),
            event_sink=collect,
        )
    )

    native_tool_names = [tool["function"]["name"] for tool in adapter.requests[0].tools]
    assert "web_search" in native_tool_names
    assert result.status == "completed"
    assert result.response_text == "Realtime answer from web_search observation."
    assert [call.tool_name for call in captured["state"].tool_calls] == ["web_search"]
    assert any(
        event.type == "run.progress"
        and event.payload.get("agent_event_type") == "progress_message"
        and event.payload.get("tool_name") == "web_search"
        for event in events
    )
    assert any(
        event.type == "tool.started" and event.payload.get("tool_name") == "web_search"
        for event in events
    )
    assert any(
        event.type == "tool.finished" and event.payload.get("tool_name") == "web_search"
        for event in events
    )
    assert any(event.type == "response.chunk" for event in events)
    assert events[-1].type == "response.final"
    assert all(event.payload.get("source") != "realtime_sla_fallback" for event in events)


def test_agent_graph_realtime_backend_emits_task_revision_progress_on_interrupt() -> None:
    captured: dict[str, UserRequest] = {}

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        captured["request"] = request
        return _completed_artifacts(request)

    backend = AgentGraphRealtimeBackend(run_request=fake_run_assistant_request)
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="runtime-run-2",
                turn_id="turn-2",
                text="等等，优先考虑降噪",
                metadata={
                    "source": "realtime_media_websocket",
                    "control": "interrupt",
                    "gateway": {"history": ["先比较耳机", "等等，优先考虑降噪"]},
                },
            ),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    assert events[0].type == "run.progress"
    assert events[0].display_only is True
    assert events[0].payload["stage"] == "task_state"
    assert events[0].payload["status"] == "revising"
    assert events[0].payload["current_step"] == "intent_revision"
    assert events[0].payload["run_id"] == "runtime-run-2"
    assert events[0].payload["turn_id"] == "turn-2"
    assert captured["request"].metadata["control"] == "interrupt"


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


def test_agent_graph_realtime_backend_emits_first_progress_fallback_before_slow_final() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        time.sleep(0.03)
        return _completed_artifacts(request, run_id="assistant-run-1", message="Done.")

    backend = AgentGraphRealtimeBackend(
        run_request=fake_run_assistant_request,
        progress_policy=ProgressPolicy(
            first_progress_timeout_s=0.01,
            heartbeat_interval_s=0,
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

    assert result.status == "completed"
    assert [event.type for event in events] == [
        "run.progress",
        "response.chunk",
        "response.final",
    ]
    fallback = events[0]
    assert fallback.text == "I am on it."
    assert fallback.display_only is True
    assert fallback.payload["source"] == "realtime_sla_fallback"
    assert fallback.payload["replaceable"] is True
    assert fallback.payload["display_only"] is True
    assert fallback.payload["stage"] == "runtime"
    assert fallback.payload["status"] == "working"
    assert fallback.payload["current_step"] == "awaiting_first_output"
    assert fallback.payload["fallback_policy_version"] == "v1"
    assert result.metadata["realtime_progress"]["sla_fallback_emitted"] is True
    assert result.metadata["realtime_progress"]["user_visible_event_count"] == 3
    assert result.metadata["realtime_progress"]["first_visible_event_ms"] >= 0


def test_agent_graph_realtime_backend_skips_first_progress_fallback_after_fast_progress() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        kwargs["event_sink"].emit(
            AgentEvent(
                type="task_started",
                session_id=request.session_id,
                run_id="assistant-run-1",
                payload={"user_id": request.user_id},
            )
        )
        time.sleep(0.03)
        return _completed_artifacts(request, run_id="assistant-run-1", message="Done.")

    backend = AgentGraphRealtimeBackend(
        run_request=fake_run_assistant_request,
        progress_policy=ProgressPolicy(
            first_progress_timeout_s=0.02,
            heartbeat_interval_s=0,
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

    assert result.status == "completed"
    assert all(event.payload.get("source") != "realtime_sla_fallback" for event in events)
    assert events[0].type == "run.progress"
    assert events[0].payload["status"] == "started"
    assert result.metadata["realtime_progress"]["sla_fallback_emitted"] is False


def test_agent_graph_realtime_backend_skips_first_progress_fallback_after_interrupt_revision() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        time.sleep(0.03)
        return _completed_artifacts(request, run_id="assistant-run-1", message="Done.")

    backend = AgentGraphRealtimeBackend(
        run_request=fake_run_assistant_request,
        progress_policy=ProgressPolicy(
            first_progress_timeout_s=0.01,
            heartbeat_interval_s=0,
        ),
    )
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-1",
                session_id="session-1",
                run_id="runtime-run-2",
                turn_id="turn-2",
                text="等等，优先考虑降噪",
                metadata={
                    "source": "realtime_media_websocket",
                    "control": "interrupt",
                    "gateway": {"history": ["先比较耳机", "等等，优先考虑降噪"]},
                },
            ),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    assert events[0].type == "run.progress"
    assert events[0].payload["stage"] == "task_state"
    assert all(event.payload.get("source") != "realtime_sla_fallback" for event in events)
    assert result.metadata["realtime_progress"]["sla_fallback_emitted"] is False


def test_agent_graph_realtime_backend_suppresses_first_progress_fallback_after_cancel() -> None:
    token = MutableCancelToken()

    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        time.sleep(0.04)
        return _completed_artifacts(request, run_id="assistant-run-1", trace_id="trace-1")

    async def run_with_cancel() -> tuple[object, list[RealtimeAgentEvent]]:
        events: list[RealtimeAgentEvent] = []

        async def collect(event: RealtimeAgentEvent) -> None:
            events.append(event)

        async def cancel_soon() -> None:
            await asyncio.sleep(0.005)
            token.cancelled = True

        backend = AgentGraphRealtimeBackend(
            run_request=fake_run_assistant_request,
            progress_policy=ProgressPolicy(
                first_progress_timeout_s=0.01,
                heartbeat_interval_s=0,
            ),
        )
        cancel_task = asyncio.create_task(cancel_soon())
        result = await backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
            cancel_token=token,
        )
        await cancel_task
        return result, events

    result, events = asyncio.run(run_with_cancel())

    assert result.status == "cancelled"
    assert [event.type for event in events] == []
    assert result.metadata["realtime_progress"]["sla_fallback_emitted"] is False
    assert result.metadata["realtime_progress"]["user_visible_event_count"] == 0


def test_agent_graph_realtime_backend_completed_result_includes_realtime_progress_metadata() -> None:
    def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
        kwargs["event_sink"].emit(
            AgentEvent(
                type="response_delta",
                session_id=request.session_id,
                run_id="assistant-run-1",
                text="Alpha",
            )
        )
        return _completed_artifacts(request, run_id="assistant-run-1", message="Alpha")

    backend = AgentGraphRealtimeBackend(
        run_request=fake_run_assistant_request,
        progress_policy=ProgressPolicy(first_progress_timeout_s=1.0),
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

    assert result.status == "completed"
    assert result.metadata["realtime_progress"] == {
        "first_visible_event_ms": result.metadata["realtime_progress"]["first_visible_event_ms"],
        "sla_fallback_emitted": False,
        "user_visible_event_count": 2,
    }
    assert result.metadata["realtime_progress"]["first_visible_event_ms"] >= 0


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
    assert result.metadata["cancel_phase"] == "pre_run"
    assert result.metadata["best_effort"] is True
    _assert_realtime_cancel_contract(
        result.metadata,
        cancelled_by="run.cancel",
        phase="before_llm",
    )


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
    assert result.metadata["assistant_run_id"] == "assistant-run-1"
    assert result.metadata["realtime_progress"] == _no_visible_realtime_progress()
    assert result.metadata["cancel_phase"] == "post_run"
    assert result.metadata["best_effort"] is True
    _assert_realtime_cancel_contract(
        result.metadata,
        cancelled_by="run.cancel",
        phase="final_streaming",
    )
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
    assert result.metadata["assistant_run_id"] == "assistant-run-1"
    assert result.metadata["realtime_progress"] == _no_visible_realtime_progress()
    assert result.metadata["cancel_source"] == "deadline"
    assert result.metadata["cancel_reason"] == "run_deadline_expired"
    assert result.metadata["deadline_ms"] == 50
    assert result.metadata["cancel_phase"] == "post_run"
    assert result.metadata["best_effort"] is True
    _assert_realtime_cancel_contract(
        result.metadata,
        cancelled_by="deadline",
        phase="final_streaming",
    )


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
    assert result.metadata["assistant_run_id"] == "assistant-run-1"
    assert result.metadata["realtime_progress"] == _no_visible_realtime_progress()
    assert result.metadata["cancel_phase"] == "after_node"
    assert result.metadata["best_effort"] is True
    _assert_realtime_cancel_contract(
        result.metadata,
        cancelled_by="run.cancel",
        phase="llm_streaming",
    )
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
    assert result.metadata["assistant_run_id"] == "assistant-run-1"
    assert result.metadata["realtime_progress"] == _no_visible_realtime_progress()
    assert result.metadata["cancel_source"] == "deadline"
    assert result.metadata["cancel_reason"] == "run_deadline_expired"
    assert result.metadata["deadline_ms"] == 75
    assert result.metadata["cancel_phase"] == "after_node"
    assert result.metadata["best_effort"] is True
    _assert_realtime_cancel_contract(
        result.metadata,
        cancelled_by="deadline",
        phase="llm_streaming",
    )


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
    assert result.metadata["error_type"] == "RuntimeError"
    assert result.metadata["error_message"] == "backend exploded"
    assert result.metadata["realtime_progress"]["sla_fallback_emitted"] is False
    assert result.metadata["realtime_progress"]["user_visible_event_count"] == 1
    assert result.metadata["realtime_progress"]["first_visible_event_ms"] >= 0
    assert [event.type for event in events] == ["error"]
    assert events[0].text == "backend exploded"
    assert events[0].payload["error_type"] == "RuntimeError"
