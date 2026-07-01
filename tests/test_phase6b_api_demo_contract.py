from fastapi.testclient import TestClient

from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.services.generated_artifacts import GENERATED_ARTIFACT_DIR


def test_demo_scenarios_endpoint_lists_offline_scenarios() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/scenarios")
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert payload["offline"] is True
    assert payload["total"] >= 8
    assert any(scenario["scenario_id"] == "product_search_compare" for scenario in payload["scenarios"])
    first = payload["scenarios"][0]
    assert {
        "scenario_id",
        "title",
        "user_query",
        "input_type",
        "expected_tools",
        "expected_response_contains",
        "mock_only",
    }.issubset(first)


def test_agent_run_response_is_demo_console_ready() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "demo_user", "session_id": "demo_session", "text": "帮我找 500 元以内的白色运动鞋，并比较价格。"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")
    assert payload["response_text"] != "已完成请求处理。"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["product_search", "price_compare"]
    assert payload["errors"] == []


def test_default_api_runtime_keeps_run_and_trace_queryable() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={"user_id": "demo_user", "session_id": "demo_session", "text": "生成一张日系极简商品海报。"},
    )
    run_payload = run_response.json()

    run_summary = client.get(f"/runs/{run_payload['run_id']}")
    trace_summary = client.get(f"/traces/{run_payload['trace_id']}")

    assert run_summary.status_code == 200
    assert trace_summary.status_code == 200
    assert run_summary.json()["run_id"] == run_payload["run_id"]
    assert trace_summary.json()["trace_id"] == run_payload["trace_id"]
    assert trace_summary.json()["events"]


def test_generated_artifacts_are_served_by_backend() -> None:
    GENERATED_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = GENERATED_ARTIFACT_DIR / "test-generated-artifact.png"
    artifact.write_bytes(b"fake-png")
    client = TestClient(create_app())

    response = client.get("/artifacts/generated/test-generated-artifact.png")

    assert response.status_code == 200
    assert response.content == b"fake-png"
