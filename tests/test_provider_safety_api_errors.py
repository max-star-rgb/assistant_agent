from fastapi.testclient import TestClient

from multimodal_agent.agent.state import AgentState
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.schemas.api import api_error
from multimodal_agent.schemas.requests import AgentResponse, UserRequest


def test_provider_api_error_redacts_sensitive_fields() -> None:
    error = api_error(
        "provider_bad_response",
        "Authorization: Bearer sk-test raw provider response body",
        detail={
            "headers": {"Authorization": "Bearer sk-test"},
            "raw_response": "raw provider response body",
            "source": "vision_understanding",
        },
    )
    dumped = error.model_dump_json()

    assert error.code == "TASK_FAILED"
    assert "sk-test" not in dumped
    assert "Authorization" not in dumped
    assert "raw_response" not in dumped


def test_budget_error_is_exposed_with_stable_api_error_shape(monkeypatch) -> None:
    class BudgetRuntime:
        def run_state(self, request: UserRequest) -> AgentState:
            state = AgentState.from_request(request)
            call = state.add_tool_call("image_generation", {"prompt": "hello"})
            state.fail_tool_call(
                call.call_id,
                "Provider call budget exceeded for this run.",
                error_details={
                    "code": "provider_call_limit_exceeded",
                    "recovery_action": "stop_with_error",
                    "retryable": False,
                    "step_id": "step_1",
                },
                stop_run=True,
            )
            state.response = AgentResponse(
                message="处理失败：provider_call_limit_exceeded: Provider call budget exceeded for this run.",
                data={"errors": [{"code": "provider_call_limit_exceeded"}]},
            )
            return state

    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: BudgetRuntime())
    client = TestClient(create_app())

    response = client.post("/agent/run", json={"user_id": "u1", "session_id": "s1", "text": "生成图片"})
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "PROVIDER_BUDGET_EXCEEDED"
    assert payload["errors"][0]["detail"]["source"] == "image_generation"
    assert "sk-" not in response.text
    assert "Authorization" not in response.text
