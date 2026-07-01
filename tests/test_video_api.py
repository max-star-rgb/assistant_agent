from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app


def test_video_api_returns_video_understanding_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "总结这个视频",
            "video_ids": ["video_api_1"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "video_understanding"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["video_understanding"]
    assert payload["errors"] == []

    result = payload["tool_results"][0]
    assert result["success"] is True
    assert result["output_ref"] == "mock://video/understanding/video_api_1"
    assert result["data"]["provider"] == "mock"
    assert result["data"]["summary"]
    assert result["contract"]["capability"] == "video_understanding"
    assert result["contract"]["status"] == "succeeded"
    assert result["contract"]["output_ref"] == "mock://video/understanding/video_api_1"


def test_video_api_returns_multistep_video_result() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "找视频里的商品并比较价格",
            "video_ids": ["video_api_2"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "multi_step_orchestration"
    assert [call["tool_name"] for call in payload["tool_calls"]] == [
        "video_understanding",
        "product_search",
        "price_compare",
    ]
    assert payload["tool_results"][0]["contract"]["capability"] == "video_understanding"
    assert payload["tool_calls"][1]["input"]["video_summary"]
