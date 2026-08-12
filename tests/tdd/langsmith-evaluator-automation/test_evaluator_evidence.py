from __future__ import annotations

from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from evals.langsmith_runtime_regression import evaluators


def test_langsmith_output_includes_bounded_evaluation_evidence() -> None:
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
        ),
        run_id="run-sentinel",
        trace_id="a" * 32,
    )
    state.status = "completed"
    state.response = AgentResponse(message="response-sentinel")

    output = evaluators.langsmith_evaluator_output(state, events=[])

    assert output["role"] == "assistant"
    assert output["content"] == "response-sentinel"
    assert output["evaluation_evidence"]["final_state"]["status"] == "completed"
    assert "user_id" not in output["evaluation_evidence"]
