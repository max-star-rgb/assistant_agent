from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_demo_flow_runs_multitool_task_and_saves_memory() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "帮我找视频里的鞋子，比较价格，然后生成一张日系海报。",
            "video_ids": ["video_demo_1"],
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["intent"] == "multi_tool_task"

    tool_names = [call["tool_name"] for call in payload["tool_calls"]]
    assert "vision_understanding" in tool_names
    assert "product_search" in tool_names
    assert "price_compare" in tool_names
    assert "image_generation" in tool_names
    assert "memory_save" in tool_names

    response_text = payload["response_text"]
    assert "商品" in response_text
    assert "最低价格" in response_text
    assert "图片生成结果" in response_text

    tool_results = payload["tool_results"]
    image_result = [result for result in tool_results if result["tool_name"] == "image_generation"][0]
    memory_result = [result for result in tool_results if result["tool_name"] == "memory_save"][0]

    assert image_result["data"]["image_url"] == "local://generated/poster.png"
    assert memory_result["success"] is True
    assert memory_result["data"]["summary"] == "已保存用户偏好。"
    assert memory_result["data"]["content"]["summary"] == "完成视频鞋子识别、商品搜索、比价和日系海报生成。"
