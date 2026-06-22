from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_websocket_task_failed_error_uses_stable_error_shape() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=哪个便宜") as websocket:
        events = _receive_until(websocket, "task_failed")

    task_failed = events[-1]
    assert task_failed["type"] == "task_failed"
    assert task_failed["error"]["code"] == "TOOL_INPUT_INVALID"
    assert task_failed["error"]["message"]
    assert task_failed["error"]["detail"]["source"] == "price_compare"
    assert task_failed["error"]["recoverable"] is False


def test_websocket_tool_failed_error_uses_same_error_shape() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=哪个便宜") as websocket:
        events = _receive_until(websocket, "task_failed")

    tool_failed = next(event for event in events if event["type"] == "tool_failed")
    assert tool_failed["type"] == "tool_failed"
    assert tool_failed["error"]["code"] == "TOOL_INPUT_INVALID"
    assert tool_failed["error"]["message"]
    assert tool_failed["error"]["detail"]["step_id"] == "step_1"
    assert tool_failed["error"]["recoverable"] is False


def _receive_until(websocket, event_type: str, limit: int = 20) -> list[dict]:
    events = []
    for _ in range(limit):
        event = websocket.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events
    raise AssertionError(f"did not receive {event_type}; got {[event['type'] for event in events]}")
