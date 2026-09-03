from assistant_agent.multi_agent.router_models import AgentRunResponse


def test_public_response_keeps_empty_tool_ledger_compatibility_fields() -> None:
    response = AgentRunResponse(
        run_id="run-sentinel",
        trace_id="trace-sentinel",
        status="completed",
        response_text="response-sentinel",
    )

    public = response.model_dump(mode="json")
    assert public["tool_calls"] == []
    assert public["tool_results"] == []
