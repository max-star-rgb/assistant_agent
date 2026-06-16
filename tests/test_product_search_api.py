from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_product_search_api_returns_stable_product_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找 500 元以内的白色运动鞋"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "product_search"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["product_search"]
    assert payload["errors"] == []

    result = payload["tool_results"][0]
    assert result["success"] is True
    assert result["data"]["provider"] == "mock"
    assert result["data"]["total"] >= 1
    product = result["data"]["items"][0]
    assert product["product_id"]
    assert product["product_url"] == "mock://shop-a/p1"
    assert product["image_url"] == "mock://images/p1.png"
    assert product["similarity_score"] is not None
    assert product["ranking_reason"]["explanation"]
    assert product["source"] == "mock"
    assert "provider_response" not in result["data"]
    assert "raw" not in result["data"]


def test_media_summary_product_search_api_runs_vision_then_search() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "找这张图里的鞋子",
            "image_ids": ["img1"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "multi_step_orchestration"
    assert [call["tool_name"] for call in payload["tool_calls"]] == [
        "vision_understanding",
        "product_search",
    ]
    search_result = payload["tool_results"][1]
    assert search_result["success"] is True
    assert search_result["data"]["items"][0]["source"] == "mock"
