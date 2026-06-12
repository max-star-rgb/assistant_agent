from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app
from multimodal_agent.api.websocket import mock_agent_events


def test_websocket_uses_graph_runtime_event_sequence() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=帮我找相似款") as websocket:
        events = [websocket.receive_json() for _ in range(6)]

    assert [event["type"] for event in events] == [
        "task_started",
        "graph_node_started",
        "tool_started",
        "tool_finished",
        "graph_node_finished",
        "final_response",
    ]
    assert {event["session_id"] for event in events} == {"s1"}
    assert events[2]["tool_name"] == "product_search"
    assert events[3]["output_ref"] == "mock://products/white-low-top-sneaker"
    assert events[-1]["run_id"].startswith("run_")


def test_websocket_emits_structured_error_event_for_failed_tool() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s2?text=哪个便宜") as websocket:
        events = [websocket.receive_json() for _ in range(6)]

    assert [event["type"] for event in events] == [
        "task_started",
        "graph_node_started",
        "tool_started",
        "tool_failed",
        "graph_node_finished",
        "task_failed",
    ]
    assert events[3]["tool_name"] == "price_compare"
    assert events[3]["error"]
    assert events[-1]["error"]


def test_mock_websocket_helper_remains_available_for_fallback_tests() -> None:
    events = mock_agent_events("fallback")

    assert [event.type for event in events] == [
        "tool_started",
        "tool_progress",
        "tool_completed",
        "agent_response",
    ]
