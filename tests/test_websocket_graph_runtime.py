from fastapi.testclient import TestClient
from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.auth import (
    AUTH_HEADER_ENABLED_ENV,
    AUTH_MODE_ENV,
    AUTH_REQUIRE_BOUND_IDENTITY_ENV,
    AUTH_SESSION_ID_HEADER,
    AUTH_USER_ID_HEADER,
)
from assistant_agent.api.app import create_app
from assistant_agent.api.websocket import mock_agent_events
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str | ChatResult]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        output = self.outputs[index]
        if isinstance(output, ChatResult):
            return output
        return ChatResult(response_text=output, provider=self.provider, model="scripted")


class StreamingFinalChatAdapter:
    provider = "streaming-scripted"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if request.stream_callback is not None:
            request.stream_callback("流式", {"provider": self.provider, "token_streaming": True})
            request.stream_callback("回答", {"provider": self.provider, "token_streaming": True})
        return ChatResult(response_text="流式回答", provider=self.provider, model="scripted-stream")


class RecordingWebSocketRuntime:
    def __init__(self) -> None:
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_websocket_auth_test")
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="test"))
        state.set_response(AgentResponse(message="websocket runtime", data={"runtime": "websocket"}))
        return state


class FailingPriceCompareInput(BaseModel):
    query: str


class FailingPriceCompareTool(MockTool):
    name = "price_compare"
    description = "Failing price compare tool for websocket error tests."
    input_schema = FailingPriceCompareInput
    output_schema = FailingPriceCompareInput

    def _run(self, input: FailingPriceCompareInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timeout")


def test_websocket_uses_graph_runtime_event_sequence() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=帮我找相似款") as websocket:
        events = _receive_until(websocket, "agent_response")

    event_types = [event["type"] for event in events]
    assert event_types[:2] == ["task_started", "graph_node_started"]
    assert "agent_trace_decision" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert "agent_trace_observation" in event_types
    assert "graph_node_finished" in event_types
    assert "final_response" in event_types
    assert event_types[-1] == "agent_response"
    assert {event["session_id"] for event in events} == {"s1"}
    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["tool_name"] == "product_search"
    assert tool_finished["output_ref"] == "mock://products/white-low-top-sneaker"
    assert events[-1]["run_id"].startswith("run_")


def test_websocket_accepts_initial_json_request_payload() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/json-payload?user_id=web_user") as websocket:
        websocket.send_json(
            {
                "text": "帮我找相似款",
                "image_ids": [],
                "video_ids": [],
                "execution_strategy": "react",
            }
        )
        events = _receive_until(websocket, "agent_response")

    assert events[0]["type"] == "task_started"
    assert events[-1]["type"] == "agent_response"
    assert events[-1]["payload"]["response"]["status"] == "completed"


def test_websocket_ignores_auth_headers_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv(AUTH_HEADER_ENABLED_ENV, raising=False)
    runtime = RecordingWebSocketRuntime()
    try:
        routes_agent._RUNTIME = runtime
        client = TestClient(create_app())

        with client.websocket_connect(
            "/ws/agent/header-disabled?text=你好&user_id=body_user",
            headers={AUTH_USER_ID_HEADER: "header_user", AUTH_SESSION_ID_HEADER: "header_session"},
        ) as websocket:
            events = _receive_until(websocket, "agent_response")
    finally:
        routes_agent._RUNTIME = None

    assert events[-1]["payload"]["response"]["status"] == "completed"
    assert runtime.requests[0].user_id == "body_user"
    assert runtime.requests[0].session_id == "header-disabled"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "websocket_query"
    assert metadata["auth_bound_identity"] is False
    assert runtime.requests[0].metadata["runtime"]["history"] == ["你好"]


def test_websocket_rejects_enabled_header_auth_user_mismatch(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    runtime = RecordingWebSocketRuntime()
    try:
        routes_agent._RUNTIME = runtime
        client = TestClient(create_app())

        with client.websocket_connect(
            "/ws/agent/header-mismatch?text=你好&user_id=body_user",
            headers={AUTH_USER_ID_HEADER: "auth_user"},
        ) as websocket:
            event = websocket.receive_json()
    finally:
        routes_agent._RUNTIME = None

    assert event["type"] == "agent_error"
    assert event["error"]["code"] == "ACCESS_DENIED"
    assert "auth context" in event["error"]["message"]
    assert runtime.requests == []


def test_websocket_rejects_request_identity_when_auth_bound_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")
    runtime = RecordingWebSocketRuntime()
    try:
        routes_agent._RUNTIME = runtime
        client = TestClient(create_app())

        with client.websocket_connect("/ws/agent/auth-required?text=你好&user_id=body_user") as websocket:
            event = websocket.receive_json()
    finally:
        routes_agent._RUNTIME = None

    assert event["type"] == "agent_error"
    assert event["error"]["code"] == "IDENTITY_NOT_AUTH_BOUND"
    assert event["error"]["detail"]["code"] == "IDENTITY_NOT_AUTH_BOUND"
    assert runtime.requests == []


def test_websocket_uses_trusted_header_auth_mode_when_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_MODE_ENV, "trusted_header")
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "true")
    runtime = RecordingWebSocketRuntime()
    try:
        routes_agent._RUNTIME = runtime
        client = TestClient(create_app())

        with client.websocket_connect(
            "/ws/agent/trusted-session?text=你好&user_id=auth_user",
            headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "trusted_session"},
        ) as websocket:
            events = _receive_until(websocket, "agent_response")
    finally:
        routes_agent._RUNTIME = None

    assert events[-1]["payload"]["response"]["status"] == "completed"
    assert runtime.requests[0].user_id == "auth_user"
    assert runtime.requests[0].session_id == "trusted_session"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_context_source"] == "header"


def test_websocket_first_event_streams_before_final_response() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/live?text=帮我找相似款") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "task_started"
    assert second["type"] == "graph_node_started"


def test_websocket_final_agent_response_includes_full_run_payload() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/final-payload?text=生成一张日系极简商品海报") as websocket:
        events = []
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] == "agent_response":
                break

    final = events[-1]
    response = final["payload"]["response"]
    assert final["text"] == response["response_text"]
    assert response["status"] == "completed"
    assert response["react_steps"]
    assert response["decision_trace"]
    assert any(step.get("tool_name") == "image_generation" for step in response["react_steps"])
    assert any(step.get("action") == "image_generation" for step in response["decision_trace"])


def test_websocket_streams_response_delta_before_agent_response() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/stream-delta?text=你好") as websocket:
        events = _receive_until(websocket, "agent_response")

    event_types = [event["type"] for event in events]
    assert "response_delta" in event_types
    assert event_types.index("response_delta") < event_types.index("agent_response")
    streamed = "".join(event.get("text") or "" for event in events if event["type"] == "response_delta")
    final_text = events[-1]["payload"]["response"]["response_text"]
    assert streamed
    assert streamed in final_text


def test_websocket_streams_native_final_answer_deltas_before_completion() -> None:
    adapter = StreamingFinalChatAdapter()
    try:
        routes_agent._RUNTIME = AgentGraphRuntime(chat_adapter=adapter)
        client = TestClient(create_app())

        with client.websocket_connect("/ws/agent/native-stream?text=你好") as websocket:
            events = _receive_until(websocket, "agent_response")
    finally:
        routes_agent._RUNTIME = None

    deltas = [event for event in events if event["type"] == "response_delta"]
    assert [event["text"] for event in deltas] == ["流式", "回答"]
    assert [event["payload"]["source"] for event in deltas] == [
        "assistant_native_final_answer",
        "assistant_native_final_answer",
    ]
    assert events.index(deltas[0]) < len(events) - 1
    assert events[-1]["payload"]["response"]["response_text"] == "流式回答"
    assert adapter.requests[0].stream_callback is not None


def test_websocket_accepts_explicit_plan_and_solve_strategy() -> None:
    try:
        routes_agent._RUNTIME = AgentGraphRuntime(
            chat_adapter=ScriptedChatAdapter(
                ["native plan websocket response"]
            )
        )
        client = TestClient(create_app())

        with client.websocket_connect(
            "/ws/agent/plan-ws?text=找白色运动鞋&execution_strategy=plan_and_solve"
        ) as websocket:
            events = _receive_until(websocket, "agent_response", limit=30)
    finally:
        routes_agent._RUNTIME = None

    response = events[-1]["payload"]["response"]
    assert response["execution_strategy"] == "plan_and_solve"
    assert response["response_text"] == "native plan websocket response"
    assert response["tool_calls"] == []
    assert response["data"]["native_runtime"] is True


def test_websocket_emits_structured_error_event_for_failed_tool() -> None:
    registry = ToolRegistry()
    registry.register(FailingPriceCompareTool())
    try:
        routes_agent._RUNTIME = AgentGraphRuntime(
            registry=registry,
            chat_adapter=ScriptedChatAdapter(
                [
                    ChatResult(
                        response_text="",
                        tool_calls=[
                            NativeToolCall(
                                id="call_1",
                                name="price_compare",
                                arguments={"query": "耳机"},
                                raw={
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "price_compare", "arguments": "{}"},
                                },
                            )
                        ],
                        provider="scripted",
                        model="scripted",
                        finish_reason="tool_calls",
                        message_kind="tool_call",
                    )
                ]
            ),
        )
        client = TestClient(create_app())

        with client.websocket_connect("/ws/agent/s2?text=哪个便宜") as websocket:
            events = _receive_until(websocket, "task_failed")
    finally:
        routes_agent._RUNTIME = None

    event_types = [event["type"] for event in events]
    assert event_types[0] == "task_started"
    assert "tool_started" in event_types
    assert "tool_failed" in event_types
    assert event_types[-1] == "task_failed"
    tool_failed = next(event for event in events if event["type"] == "tool_failed")
    assert tool_failed["tool_name"] == "price_compare"
    assert tool_failed["error"]
    assert events[-1]["error"]


def test_websocket_emits_render_tool_events() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s3?text=把浅灰色沙发放到北欧风客厅看看") as websocket:
        events = _receive_until(websocket, "agent_response")

    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["tool_name"] == "render_3d"
    assert tool_finished["tool_name"] == "render_3d"
    assert tool_finished["output_ref"] == "mock://render/preview.png"


def test_websocket_emits_video_understanding_events() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s4?text=总结这个视频&video_id=video_ws_1") as websocket:
        events = _receive_until(websocket, "agent_response")

    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["tool_name"] == "video_understanding"
    assert tool_finished["tool_name"] == "video_understanding"
    assert tool_finished["output_ref"] == "mock://video/understanding/video_ws_1"
    assert tool_finished["payload"]["contract"]["capability"] == "video_understanding"


def test_mock_websocket_helper_remains_available_for_fallback_tests() -> None:
    events = mock_agent_events("fallback")

    assert [event.type for event in events] == [
        "tool_started",
        "tool_progress",
        "tool_completed",
        "agent_response",
    ]


def _receive_until(websocket, event_type: str, limit: int = 20) -> list[dict]:
    events = []
    for _ in range(limit):
        event = websocket.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events
    raise AssertionError(f"did not receive {event_type}; got {[event['type'] for event in events]}")
