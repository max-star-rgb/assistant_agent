from importlib import import_module

import pytest


def test_agent_run_response_is_owned_by_multi_agent() -> None:
    module = import_module("assistant_agent.multi_agent.router_models")
    response = module.AgentRunResponse(
        run_id="run-sentinel",
        trace_id="trace-sentinel",
        status="completed",
        response_text="response-sentinel",
    )

    assert response.tool_calls == []
    assert response.tool_results == []

    with pytest.raises(ModuleNotFoundError) as caught:
        import_module("assistant_agent.api.models")
    assert caught.value.name == "assistant_agent.api.models"
