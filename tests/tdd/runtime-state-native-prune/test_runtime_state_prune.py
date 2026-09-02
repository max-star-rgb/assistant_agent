from assistant_agent.api.models import agent_run_response_from_state
from assistant_agent.observability.trace_store import summarize_graph_state
from assistant_agent.observability.turn_summary import build_turn_summary_from_state
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState


def test_legacy_tool_ledger_is_not_restored_into_runtime_state() -> None:
    state = AgentState.model_validate(
        {
            "request": UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="request-sentinel",
            ),
            "run_tool_catalog": {
                "available_tool_names": ["legacy-tool-sentinel"],
            },
            "tool_calls": [
                {
                    "tool_call_id": "call-sentinel",
                    "tool_name": "legacy-tool-sentinel",
                    "input": {"secret": "input-sentinel"},
                    "status": "running",
                    "started_at": "2026-09-02T00:00:00Z",
                }
            ],
            "tool_results": [
                {
                    "tool_name": "legacy-tool-sentinel",
                    "success": True,
                    "data": {"secret": "result-sentinel"},
                }
            ],
        }
    )

    dumped = state.model_dump(mode="json")
    assert "run_tool_catalog" not in dumped
    assert "tool_calls" not in dumped
    assert "tool_results" not in dumped

    response = agent_run_response_from_state(state)
    assert response.tool_calls == []
    assert response.tool_results == []
    assert build_turn_summary_from_state(state).tool_count == 0
    assert summarize_graph_state({"state": state}) == {
        "status": "created",
        "tool_call_count": 0,
        "tool_result_count": 0,
        "error_count": 0,
        "current_step_index": 0,
    }
