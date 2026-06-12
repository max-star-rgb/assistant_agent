from fastapi.testclient import TestClient

from multimodal_agent.agent.state import AgentState
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
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
