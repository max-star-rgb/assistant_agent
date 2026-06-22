from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app
from multimodal_agent.api.websocket import mock_agent_events


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


def test_websocket_emits_structured_error_event_for_failed_tool() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s2?text=哪个便宜") as websocket:
        events = _receive_until(websocket, "task_failed")

    event_types = [event["type"] for event in events]
    assert event_types[:2] == ["task_started", "graph_node_started"]
    assert "agent_trace_decision" in event_types
    assert "tool_started" in event_types
    assert "tool_failed" in event_types
    assert "agent_trace_observation" in event_types
    assert "graph_node_finished" in event_types
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
