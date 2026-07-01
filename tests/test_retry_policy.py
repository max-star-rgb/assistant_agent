from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_policy import ProviderExecutionPolicy, RetryPolicy
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class RetryInput(BaseModel):
    text: str


class FlakyTimeoutTool(MockTool):
    name = "flaky_timeout"
    description = "Fails once with provider_timeout, then succeeds."
    input_schema = RetryInput
    output_schema = RetryInput

    def __init__(self) -> None:
        self.calls = 0

    def _run(self, input: RetryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timed out")
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text})


class UnconfiguredTool(MockTool):
    name = "unconfigured"
    description = "Always fails with provider_unconfigured."
    input_schema = RetryInput
    output_schema = RetryInput

    def __init__(self) -> None:
        self.calls = 0

    def _run(self, input: RetryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.name, success=False, error="provider_unconfigured: missing API key")


class AuthFailedTool(UnconfiguredTool):
    name = "auth_failed"

    def _run(self, input: RetryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.name, success=False, error="provider_auth_failed: bad credentials")


def test_provider_timeout_is_retried_and_can_succeed() -> None:
    tool = FlakyTimeoutTool()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    registry = ToolRegistry()
    registry.register(tool)

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is True
    assert tool.calls == 2
    assert state.tool_calls[0].status == "succeeded"
    assert state.errors == []


def test_provider_unconfigured_is_not_retried() -> None:
    tool = UnconfiguredTool()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    registry = ToolRegistry()
    registry.register(tool)

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is False
    assert tool.calls == 1
    assert state.errors[0].details["code"] == "provider_unconfigured"
    assert state.errors[0].details["retry_count"] == 0


def test_provider_auth_failed_is_not_retried() -> None:
    tool = AuthFailedTool()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    registry = ToolRegistry()
    registry.register(tool)

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is False
    assert tool.calls == 1
    assert state.errors[0].details["code"] == "provider_auth_failed"


def test_retry_policy_honors_configured_max_retries() -> None:
    policy = ProviderExecutionPolicy(retry=RetryPolicy(max_retries=0))
    tool = FlakyTimeoutTool()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    registry = ToolRegistry()
    registry.register(tool)

    result = ToolExecutor(registry=registry, execution_policy=policy).run_tool(
        state, "step_1", tool.name, {"text": "hello"}
    )

    assert result.success is False
    assert tool.calls == 1
    assert state.errors[0].details["retry_count"] == 0
