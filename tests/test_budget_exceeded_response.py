from assistant_agent.agent.response_composer import compose_response
from assistant_agent.agent.state import AgentError, AgentState
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_budget import ProviderCallBudget


def test_response_composer_summarizes_budget_failure_with_partial_result() -> None:
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="生成图片并搜索商品"))
    state.provider_budget = ProviderCallBudget(max_provider_calls_per_run=1)
    state.provider_budget.record_call(
        run_id=state.run_id,
        capability="image_generation",
        provider="mock",
        status="succeeded",
    )
    state.tool_results.append(
        ToolResult(
            tool_name="image_generation",
            success=True,
            output_ref="mock://image/result.png",
            data={"image_url": "mock://image/result.png"},
            contract=build_capability_output_contract(
                capability="image_generation",
                status="succeeded",
                output_ref="mock://image/result.png",
                data={"image_url": "mock://image/result.png"},
            ),
        )
    )
    state.errors.append(
        AgentError(
            message="Provider call budget exceeded for this run.",
            source="product_search",
            details={
                "code": "provider_call_limit_exceeded",
                "recovery_action": "continue_with_partial_result",
                "optional_step": True,
            },
        )
    )

    response = compose_response(state)

    assert response.data["partial_success"] is True
    assert response.data["provider_budget"]["provider_call_count"] == 1
    assert "部分步骤失败" in response.message
    assert "provider_call_limit_exceeded" in response.message
