from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_health_endpoint() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_run_handles_video_product_compare_request() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "帮我找视频里的鞋子并比价",
            "video_ids": ["v1"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"].startswith("run_")
    assert payload["status"] == "completed"
    assert payload["intent"] == "multi_tool_task"
    assert payload["response_text"]

    tool_calls = payload["tool_calls"]
    tool_names = [call["tool_name"] for call in tool_calls]
    assert "vision_understanding" in tool_names
    assert "product_search" in tool_names
    assert "price_compare" in tool_names
    assert all(call["status"] == "succeeded" for call in tool_calls)
    assert all(call["call_id"].startswith("call_") for call in tool_calls)
