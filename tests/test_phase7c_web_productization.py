from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return ChatResult(response_text=self.outputs[index], provider=self.provider, model="scripted")


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


def test_phase7c_legacy_browser_console_is_removed() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")
    route_paths = {getattr(route, "path", "") for route in client.app.routes}

    assert response.status_code == 404
    assert "/demo/console" not in route_paths
    assert "/static" not in route_paths


def test_phase7c_run_trace_detail_endpoints_support_http_debug_flow() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={
            "user_id": "http_debug_user",
            "session_id": "http_debug_productization",
            "text": "生成一张日系极简商品海报。",
            "metadata": {"source": "http_agent_run_debug", "offline": True},
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
    assert any(step.get("reason") for step in run_payload["react_steps"])
    assert any(step.get("decision_summary") for step in run_payload["decision_trace"])
    assert all("thought" not in key.lower() for step in run_payload["react_steps"] for key in step)
    assert all("thought" not in key.lower() for step in run_payload["decision_trace"] for key in step)
    assert "api_key" not in run_response.text.lower()
    assert "authorization" not in run_response.text.lower()
    assert "bearer" not in run_response.text.lower()
    assert run_detail["run_id"] == run_payload["run_id"]
    assert run_detail["event_count"] > 0
    assert trace_detail["trace_id"] == run_payload["trace_id"]
    assert trace_detail["events"]
    assert tool_detail["run_id"] == run_payload["run_id"]
    assert "image_generation" in [call["tool_name"] for call in tool_detail["tool_calls"]]


def test_agent_run_accepts_explicit_plan_and_solve_strategy() -> None:
    try:
        routes_agent._RUNTIME = AgentGraphRuntime(
            chat_adapter=ScriptedChatAdapter(["native plan strategy response"])
        )
        client = TestClient(create_app())

        response = client.post(
            "/agent/run",
            json={
                "user_id": "http_debug_user",
                "session_id": "http_debug_plan_strategy",
                "text": "找白色运动鞋",
                "execution_strategy": "plan_and_solve",
            },
        )
    finally:
        routes_agent._RUNTIME = None

    payload = response.json()
    assert response.status_code == 200
    assert payload["execution_strategy"] == "plan_and_solve"
    assert payload["response_text"] == "native plan strategy response"
    assert payload["tool_calls"] == []
    assert payload["data"]["native_runtime"] is True


def test_phase7c_legacy_ws_agent_route_is_removed() -> None:
    client = TestClient(create_app())
    route_paths = {getattr(route, "path", "") for route in client.app.routes}

    assert "/ws/agent/{session_id}" not in route_paths
