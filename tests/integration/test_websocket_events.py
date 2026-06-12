from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_agent_websocket_sends_graph_runtime_events() -> None:
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
    assert all(event["session_id"] == "s1" for event in events)
    assert events[2]["tool_name"] == "product_search"
    assert events[3]["output_ref"] == "mock://products/white-low-top-sneaker"
    assert events[-1]["text"]


def test_websocket_event_run_id_is_stable_for_session() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s2?text=帮我找相似款") as websocket:
        events = [websocket.receive_json() for _ in range(6)]

    run_ids = {event["run_id"] for event in events}
    assert len(run_ids) == 1
    assert next(iter(run_ids)).startswith("run_")
