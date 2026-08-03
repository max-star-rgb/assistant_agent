from __future__ import annotations

from datetime import datetime, timezone
import json

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.provider_policy import ProviderExecutionPolicy, RetryPolicy
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import AssistantToolCall, NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import RunToolCatalog
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageExploreRequest,
    WebPageGuidanceError,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.tools import (
    WebPageExploreTool,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry
from tests.core.support import ScriptedChatAdapter


class _TimeoutExploreBackend:
    def __init__(self) -> None:
        self.attempts = 0

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        raise AssertionError("inspect is not part of this test")

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        self.attempts += 1
        return WebPageGuidanceResult(
            outcome="failed",
            url="https://example.com/service",
            requested_url="https://example.com/service",
            final_url=None,
            checked_at=datetime.now(timezone.utc),
            browser_session_id=request.browser_session_id,
            errors=[
                WebPageGuidanceError(
                    code="page_timeout",
                    message="page_timeout",
                    recoverable=True,
                )
            ],
        )


def test_explore_page_timeout_is_not_auto_retried_or_classified_as_provider_retry() -> None:
    backend = _TimeoutExploreBackend()
    registry = ToolRegistry()
    registry.register(WebPageExploreTool(backend))
    registry.seal()
    request = UserRequest(
        user_id="user-a",
        session_id="session-a",
        text="inspect",
    )
    state = AgentState.from_request(request)
    result = ToolExecutor(
        registry=registry,
        execution_policy=ProviderExecutionPolicy(
            retry=RetryPolicy(
                max_retries=3,
                retry_on=("provider_timeout",),
            )
        ),
    ).run_tool(
        state,
        "step-1",
        "web_page_explore",
        {
            "browser_session_id": "opaque-browser-session-1",
            "action": "inspect",
        },
        failure_mode="continue_to_model",
    )

    assert result.success is False
    assert backend.attempts == 1
    assert state.errors[-1].details["code"] == "page_timeout"
    assert state.errors[-1].details["retry_count"] == 0


def test_unverified_url_stays_failed_through_registry_validator_and_executor() -> None:
    config = ProviderConfig(
        provider_mode="mock",
        website_guidance_enabled=True,
        langgraph_checkpointer_backend="none",
    )
    registry = create_default_registry(config)
    request = UserRequest(
        user_id="user-a",
        session_id="session-a",
        text="查看这个页面",
    )
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=["web_page_inspect"],
    )
    decision = AssistantToolCall(
        tool_name="web_page_inspect",
        tool_input={
            "url": "https://unverified.example/service",
            "goal": "查找办理入口",
        },
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    assert validation.accepted is True

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        decision.tool_name,
        decision.tool_input,
        validated_input=validation.validated_input,
        failure_mode="continue_to_model",
    )

    assert result.success is False
    assert result.data["outcome"] == "blocked"
    assert result.data["final_url"] is None
    assert result.model_observation["is_complete"] is False
    assert result.model_observation["errors"][0]["code"] == "mock_url_unverified"


def test_enabled_mock_assistant_loop_observes_unverified_url_as_failure() -> None:
    config = ProviderConfig(
        provider_mode="mock",
        website_guidance_enabled=True,
        langgraph_checkpointer_backend="none",
    )
    registry = create_default_registry(config)
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-web-inspect",
                        name="web_page_inspect",
                        arguments={
                            "url": "https://unverified.example/service",
                            "goal": "查找办理入口",
                        },
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="该 URL 未通过页面验证，因此不能声称页面办理入口可用。",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=registry,
        config=config,
        chat_adapter=adapter,
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-a",
                session_id="session-a",
                text="请确认这个页面能否办理业务。",
            )
        )
    finally:
        runtime.close()

    assert state.status == "completed"
    assert state.tool_results[0].success is False
    tool_message = next(
        message
        for message in adapter.requests[1].messages
        if message.get("role") == "tool"
    )
    observation = json.loads(str(tool_message["content"]))
    assert observation["status"] == "failed"
    assert observation["data"]["final_url"] is None
    assert state.response is not None
    assert state.response.data["degraded"] is True
