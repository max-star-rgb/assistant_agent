from fastapi.testclient import TestClient

from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app


def test_demo_runtime_info_is_redacted_and_offline_by_default() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    response = client.get("/demo/runtime-info")
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert payload["runtime_profile"] == "local_demo"
    assert payload["graph_mode"] == "assistant_loop"
    assert payload["offline_default"] is True
    assert payload["providers"]["chat"] == "mock"
    assert "api_key" not in response.text.lower()
    assert "authorization" not in response.text.lower()
    assert "bearer" not in response.text.lower()


def test_phase7c_console_contains_productized_web_controls() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")
    html = response.text

    assert response.status_code == 200
    assert "Assistant Chat" in html
    assert "Trial User" in html
    assert "trial-user-id" in html
    assert "multimodal_agent_trial_user_id" in html
    assert "Examples" in html
    assert "Input" in html
    assert "Conversation History" in html
    assert "Assistant ReAct Process" in html
    assert "conversationHistory" in html
    assert "renderConversationHistory" in html
    assert "runAssistantStream" in html
    assert "Live events" in html
    assert "formatDecisionTrace" in html
    assert "Final Decision Trace" in html
    assert "formatReactSteps" not in html
    assert "new WebSocket" in html
    assert "params.set(\"user_id\", userId)" in html
    assert "/ws/agent/" in html
    assert "Run detail panel" not in html
    assert "Trace detail panel" not in html
    assert "Browser-session request history" not in html


def test_phase7c_run_trace_detail_endpoints_support_console_flow() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={
            "user_id": "web_demo_user",
            "session_id": "web_demo_productization",
            "text": "生成一张日系极简商品海报。",
            "metadata": {"source": "web_console", "offline": True},
        },
    )
    run_payload = run_response.json()

    run_detail = client.get(f"/runs/{run_payload['run_id']}").json()
    trace_detail = client.get(f"/traces/{run_payload['trace_id']}").json()
    tool_detail = client.get(f"/runs/{run_payload['run_id']}/tool-calls").json()

    assert run_response.status_code == 200
    assert run_payload["errors"] == []
    assert run_payload["react_steps"]
    assert run_payload["decision_trace"]
    assert any(step.get("decision_type") == "tool_call" for step in run_payload["react_steps"])
    assert any(step.get("observation_tool") == "image_generation" for step in run_payload["react_steps"])
    assert any(step.get("event") == "decision" for step in run_payload["decision_trace"])
    assert any(step.get("event") == "observation" for step in run_payload["decision_trace"])
    assert "api_key" not in run_response.text.lower()
    assert "authorization" not in run_response.text.lower()
    assert "bearer" not in run_response.text.lower()
    assert run_detail["run_id"] == run_payload["run_id"]
    assert run_detail["event_count"] > 0
    assert trace_detail["trace_id"] == run_payload["trace_id"]
    assert trace_detail["events"]
    assert tool_detail["run_id"] == run_payload["run_id"]
    assert "image_generation" in [call["tool_name"] for call in tool_detail["tool_calls"]]


def test_phase7c_websocket_run_is_queryable_via_shared_http_runtime() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/ws_shared?text=帮我找相似款") as websocket:
        final = None
        while True:
            event = websocket.receive_json()
            if event["type"] == "agent_response" and event.get("payload", {}).get("response"):
                final = event["payload"]["response"]
                break

    assert final is not None
    run_id = final["run_id"]
    trace_id = final["trace_id"]

    # WS and HTTP share the same singleton runtime, so the WS run resolves here.
    run_detail = client.get(f"/runs/{run_id}")
    trace_detail = client.get(f"/traces/{trace_id}")
    tool_detail = client.get(f"/runs/{run_id}/tool-calls")

    assert run_detail.status_code == 200
    assert run_detail.json()["run_id"] == run_id
    assert trace_detail.status_code == 200
    assert trace_detail.json()["trace_id"] == trace_id
    assert tool_detail.status_code == 200
    assert tool_detail.json()["run_id"] == run_id
