from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ApprovalPolicy, ExecutionPolicy, ToolPolicyMetadata, ToolResult
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
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(retry_count=2),
    )

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
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(retry_count=2),
    )

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


class NoRetryReadTool(FlakyTimeoutTool):
    name = "no_retry_read"
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(retry_count=0),
    )


class AlwaysTimeoutReadTool(FlakyTimeoutTool):
    name = "always_timeout_read"
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(retry_count=1),
    )

    def _run(self, input: RetryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timed out")


class MutatingTimeoutTool(FlakyTimeoutTool):
    name = "mutating_timeout"
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="always"),
        execution=ExecutionPolicy(retry_count=2, idempotency="none"),
    )


class IdempotentMutatingTimeoutTool(FlakyTimeoutTool):
    name = "idempotent_mutating_timeout"
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="always"),
        execution=ExecutionPolicy(retry_count=2, idempotency="required"),
    )


class LegacyReadTimeoutTool(FlakyTimeoutTool):
    name = "shopping_search"
    policy = None


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


def test_rich_read_only_retry_count_zero_disables_retry() -> None:
    tool = NoRetryReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is False
    assert tool.calls == 1
    assert state.errors[0].details["retry_count"] == 0


def test_rich_retry_count_caps_the_global_retry_limit() -> None:
    policy = ProviderExecutionPolicy(retry=RetryPolicy(max_retries=3))
    tool = AlwaysTimeoutReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry, execution_policy=policy).run_tool(
        state, "step_1", tool.name, {"text": "hello"}
    )

    assert result.success is False
    assert tool.calls == 2
    assert state.errors[0].details["retry_count"] == 1


def test_non_idempotent_mutation_is_not_retried() -> None:
    tool = MutatingTimeoutTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = _confirmed_state(tool.name)

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is False
    assert tool.calls == 1
    assert result.data["status"] == "unknown_after_timeout"
    assert state.errors[0].details["retry_count"] == 0


def test_idempotency_protected_mutation_can_retry() -> None:
    tool = IdempotentMutatingTimeoutTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = _confirmed_state(tool.name)

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step_1",
        tool.name,
        {"text": "hello", "idempotency_key": "mutation-1"},
    )

    assert result.success is True
    assert tool.calls == 2


def test_legacy_read_only_tool_keeps_global_retry_behavior() -> None:
    tool = LegacyReadTimeoutTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))

    result = ToolExecutor(registry=registry).run_tool(state, "step_1", tool.name, {"text": "hello"})

    assert result.success is True
    assert tool.calls == 2


def _confirmed_state(tool_name: str) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="confirm",
            metadata={
                "tool_confirmation": {
                    "tool_name": tool_name,
                    "confirmed": True,
                    "confirmed_by": "user",
                }
            },
        )
    )
