from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_websocket_task_failed_error_uses_stable_error_shape() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=哪个便宜") as websocket:
        events = [websocket.receive_json() for _ in range(6)]

    task_failed = events[-1]
    assert task_failed["type"] == "task_failed"
    assert task_failed["error"]["code"] == "TOOL_INPUT_INVALID"
    assert task_failed["error"]["message"]
    assert task_failed["error"]["detail"]["source"] == "price_compare"
    assert task_failed["error"]["recoverable"] is False


def test_websocket_tool_failed_error_uses_same_error_shape() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/s1?text=哪个便宜") as websocket:
        events = [websocket.receive_json() for _ in range(6)]

    tool_failed = events[3]
    assert tool_failed["type"] == "tool_failed"
    assert tool_failed["error"]["code"] == "TOOL_INPUT_INVALID"
    assert tool_failed["error"]["message"]
    assert tool_failed["error"]["detail"]["step_id"] == "step_1"
    assert tool_failed["error"]["recoverable"] is False
