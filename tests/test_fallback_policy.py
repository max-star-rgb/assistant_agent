from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ApprovalPolicy, ToolPolicyMetadata, ToolResult
from assistant_agent.services.provider_policy import FallbackPolicy, ProviderExecutionPolicy, RetryPolicy
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class FallbackInput(BaseModel):
    text: str


class FailingProviderTool(MockTool):
    name = "failing_provider"
    description = "Fails like a provider adapter."
    input_schema = FallbackInput
    output_schema = FallbackInput
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
    )

    def _run(self, input: FallbackInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="provider_timeout: upstream timed out",
            output_ref=None,
        )


def test_mock_fallback_is_disabled_by_default() -> None:
    policy = FallbackPolicy()

    assert policy.allow_mock_fallback is False
    assert policy.allow_partial_result is True


def test_provider_failure_does_not_silently_fallback_to_mock() -> None:
    registry = ToolRegistry()
    registry.register(FailingProviderTool())
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    execution_policy = ProviderExecutionPolicy(
        retry=RetryPolicy(max_retries=0),
        fallback=FallbackPolicy(allow_mock_fallback=False),
    )

    result = ToolExecutor(registry=registry, execution_policy=execution_policy).run_tool(
        state,
        "step_1",
        "failing_provider",
        {"text": "hello"},
    )

    assert result.success is False
    assert result.output_ref is None
    assert state.status == "failed"
    assert state.tool_calls[0].status == "failed"
    assert "mock://" not in (state.tool_results[0].output_ref or "")


def test_mock_fallback_env_flag_is_explicit() -> None:
    policy = FallbackPolicy.from_env({"MULTIMODAL_AGENT_ALLOW_MOCK_FALLBACK": "1"})

    assert policy.allow_mock_fallback is True
