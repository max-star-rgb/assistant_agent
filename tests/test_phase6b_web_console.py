from fastapi.testclient import TestClient

import assistant_agent.api.app as app_module
from assistant_agent.api.app import create_app


def test_demo_console_page_is_not_served() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")

    assert response.status_code == 404


def test_static_console_asset_is_not_served() -> None:
    client = TestClient(create_app())

    response = client.get("/static/index.html")

    assert response.status_code == 404


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
