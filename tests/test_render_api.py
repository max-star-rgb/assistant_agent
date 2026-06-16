from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app


def test_render_api_returns_render_contract() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "把浅灰色沙发放到北欧风客厅看看"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "render_3d"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["render_3d"]
    assert payload["errors"] == []

    result = payload["tool_results"][0]
    assert result["success"] is True
    assert result["output_ref"] == "mock://render/preview.png"
    assert result["data"]["provider"] == "mock"
    assert result["data"]["status"] == "succeeded"
    assert result["data"]["output_ref"] == "mock://render/preview.png"
    assert result["data"]["preview_url"] == "mock://render/preview.png"
    assert result["data"]["render_id"] == "mock_render_task_1"
    assert result["data"]["scene_description"] == "把浅灰色沙发放到北欧风客厅看看"
    assert result["data"]["used_inputs"]["scene_description"] == "把浅灰色沙发放到北欧风客厅看看"


def test_render_api_returns_multistep_render_result() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找一款黑色办公椅，然后放到现代办公室里看看"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["intent"] == "multi_step_orchestration"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["product_search", "render_3d"]

    render_call = payload["tool_calls"][1]
    render_result = payload["tool_results"][1]
    assert render_call["input"]["product_ref"] == "p1"
    assert render_call["input"]["product_title"] == "白色低帮运动鞋 A"
    assert render_result["success"] is True
    assert render_result["data"]["provider"] == "mock"
    assert render_result["output_ref"] == "mock://render/preview.png"
