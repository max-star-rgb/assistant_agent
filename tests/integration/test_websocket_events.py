from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def _receive_until(websocket, event_type: str, limit: int = 20) -> list[dict]:
    events = []
    for _ in range(limit):
        event = websocket.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events
    raise AssertionError(f"did not receive {event_type}; got {[event['type'] for event in events]}")


def test_agent_websocket_sends_graph_runtime_events() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=帮我找相似款") as websocket:
        events = _receive_until(websocket, "final_response")

    event_types = [event["type"] for event in events]
    assert event_types[:2] == ["task_started", "graph_node_started"]
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert "graph_node_finished" in event_types
    assert event_types[-1] == "final_response"
    assert all(event["session_id"] == "s1" for event in events)
    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["tool_name"] == "product_search"
    assert tool_finished["output_ref"] == "mock://products/white-low-top-sneaker"
    assert events[-1]["text"]


def test_websocket_event_run_id_is_stable_for_session() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s2?text=帮我找相似款") as websocket:
        events = _receive_until(websocket, "final_response")

    run_ids = {event["run_id"] for event in events if event.get("run_id")}
    assert len(run_ids) == 1
    assert next(iter(run_ids)).startswith("run_")
