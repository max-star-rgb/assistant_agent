from fastapi.testclient import TestClient

from multimodal_agent.agent.state import AgentState
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.schemas.api import AgentRunResponse
from multimodal_agent.schemas.planning import IntentResult
from multimodal_agent.schemas.requests import AgentResponse, UserRequest


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_graph_runtime_test")
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="test"))
        state.set_response(AgentResponse(message="graph runtime", data={"runtime": "graph"}))
        return state


class RecordingGateway:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def run(self, request) -> AgentRunResponse:
        self.requests.append(request)
        return AgentRunResponse(
            run_id="run_gateway_test",
            trace_id="trace_gateway_test",
            status="completed",
            intent="chat",
            response_text="gateway runtime",
            data={
                "agent_gateway": {
                    "agent_id": request.target_agent_id or "agent.default",
                    "collaboration_mode": request.effective_collaboration_mode(),
                }
            },
            runtime_info={"agent_gateway": {"offline": True}},
        )


def test_api_agent_run_defaults_to_graph_runtime(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run_graph_runtime_test"
    assert payload["response_text"] == "graph runtime"
    assert payload["intent"] == "chat"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].metadata["request_identity"]["identity_source"] == "request_body"
    assert runtime.requests[0].metadata["request_identity"]["auth_bound_identity"] is False


def test_api_agents_run_uses_agent_gateway(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_agent, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/agents/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "你好",
            "target_agent_id": "agent.worker",
            "collaboration_mode": "single",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run_gateway_test"
    assert payload["response_text"] == "gateway runtime"
    assert payload["data"]["agent_gateway"]["agent_id"] == "agent.worker"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].metadata["request_identity"]["identity_source"] == "request_body"


def test_api_agent_run_does_not_use_agent_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(
        routes_agent,
        "get_agent_gateway",
        lambda: (_ for _ in ()).throw(AssertionError("gateway should not be used")),
    )
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run_graph_runtime_test"
    assert len(runtime.requests) == 1
