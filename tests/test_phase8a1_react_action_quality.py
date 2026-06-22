from typing import Any

from pydantic import BaseModel

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.assistant_decision import AssistantDecision
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult
from multimodal_agent.services.trace_store import InMemoryTraceStore
from multimodal_agent.tools.base import MockTool, ToolContext
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return ChatResult(response_text=self.outputs[index], provider=self.provider, model="scripted")


class FailingInput(BaseModel):
    query: str | None = None


class AlwaysFailTool(MockTool):
    name = "product_search"
    description = "Always fails for loop guard tests."
    input_schema = FailingInput
    output_schema = FailingInput

    def _run(self, input: FailingInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timeout")


def test_action_spec_view_includes_usage_guidance() -> None:
    descriptions = create_default_registry().describe_tools()
    render = next(item for item in descriptions if item["name"] == "render_3d")

    assert render["when_to_use"]
    assert any("描述" in item or "describe" in item.lower() for item in render["when_not_to_use"])
    assert "runtime_constraints" in render


def test_assistant_decision_rejects_non_dict_tool_input() -> None:
    decision = AssistantDecision.from_llm_output(
        '{"type": "tool_call", "tool_name": "image_generation", "tool_input": "bad"}'
    )

    assert decision.type == "final_answer"
    assert "invalid_tool_input" in decision.safety_notes


def test_unknown_tool_is_rejected_and_traced() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            ['{"type": "tool_call", "tool_name": "unknown_tool", "tool_input": {}, "reason": "test"}']
        ),
        trace_store=trace_store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use unknown"))

    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "unknown_tool"
    assert state.request.metadata["assistant_loop_steps"][1]["status"] == "rejected"
    event_types = [event.event_type for event in trace_store.list_by_run(state.run_id)]
    assert "action_rejected" in event_types
    assert "loop_guard_triggered" in event_types


def test_invalid_tool_input_is_rejected_before_execution() -> None:
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            [
                (
                    '{"type": "tool_call", "tool_name": "image_generation", '
                    '"tool_input": {}, "reason": "missing prompt"}'
                )
            ]
        )
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="generate image"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"


def test_tool_failure_creates_observation_and_same_tool_guard_trace() -> None:
    registry = ToolRegistry()
    registry.register(AlwaysFailTool())
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        registry=registry,
        chat_adapter=ScriptedChatAdapter(
            ['{"type": "tool_call", "tool_name": "product_search", "tool_input": {"query": "鞋"}, "reason": "search"}']
        ),
        trace_store=trace_store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="找鞋"))

    observations = [
        step for step in state.request.metadata["assistant_loop_steps"] if step.get("observation_tool") == "product_search"
    ]
    assert observations
    assert observations[0]["status"] == "failed"
    assert observations[0]["summary"]
    assert observations[0]["next_step_hint"]
    assert any(event.event_type == "tool_observation" for event in trace_store.list_by_run(state.run_id))
    assert any(event.event_type == "loop_guard_triggered" for event in trace_store.list_by_run(state.run_id))


def test_render_scene_description_negative_guard_still_holds() -> None:
    state = AgentGraphRuntime().run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="图里是什么？请简要描述主要物体、颜色、材质和场景。",
            image_ids=["img1"],
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["vision_understanding"]
    assert "render_3d" not in [call.tool_name for call in state.tool_calls]


def test_offline_default_uses_mock_provider_without_real_call() -> None:
    state = AgentGraphRuntime().run_state(UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍"))

    assert state.response is not None
    assert state.response.data["provider"] == "mock"
    assert state.tool_calls == []
