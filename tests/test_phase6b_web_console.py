from fastapi.testclient import TestClient

import multimodal_agent.api.app as app_module
from multimodal_agent.api.app import create_app


def test_demo_console_page_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")

    assert response.status_code == 200
    assert "Assistant Chat" in response.text
    assert "请输入你的工号：00xxxx" in response.text
    assert "new WebSocket" in response.text
    assert "/ws/agent/" in response.text


def test_static_console_asset_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 200
    assert "Examples" in response.text
    assert "Conversation History" in response.text
    assert "Product Results" in response.text
    assert "Generated Images" in response.text
    assert "Assistant ReAct Process" in response.text
    assert "/demo/access" in response.text
    assert "/demo/examples" in response.text
    assert "renderExamples" in response.text
    assert "validateTrialUserId" in response.text
    assert "collectProductResults" in response.text
    assert "renderProductGallery" in response.text
    assert "collectImageArtifacts" in response.text
    assert "normalizeArtifactUrl" in response.text


def test_generated_artifact_static_path_is_served(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "GENERATED_ARTIFACT_DIR", tmp_path)
    (tmp_path / "sample.png").write_bytes(b"fake-png")
    client = TestClient(create_app())

    response = client.get("/artifacts/generated/sample.png")

    assert response.status_code == 200
    assert response.content == b"fake-png"
    assert response.headers["content-type"] == "image/png"


def test_demo_examples_endpoint_serves_shared_examples() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/examples")
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert "帮我购买乐事薯片，先搜索商品并比较价格，给出购买建议。" in payload["examples"]
