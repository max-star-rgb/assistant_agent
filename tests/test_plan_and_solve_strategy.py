from pydantic import BaseModel

from multimodal_agent.agent.plan_validator import PlanValidator
from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.planning import TaskPlan, TaskStep
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult
from multimodal_agent.services.trace_store import InMemoryTraceStore
from multimodal_agent.tools.base import MockTool, ToolContext
from multimodal_agent.tools.registry import ToolRegistry


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return ChatResult(response_text=self.outputs[index], provider=self.provider, model="scripted")


class EchoInput(BaseModel):
    text: str


class EchoTool(MockTool):
    name = "echo"
    description = "Echo text for plan-and-solve tests."
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text}, output_ref=f"echo://{input.text}")


class FailingInput(BaseModel):
    query: str


class AlwaysFailTool(MockTool):
    name = "unstable_search"
    description = "Always fails for replan tests."
    input_schema = FailingInput
    output_schema = FailingInput

    def _run(self, input: FailingInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, error="provider_timeout: timeout")


def test_default_execution_strategy_remains_react() -> None:
    adapter = ScriptedChatAdapter(['{"type": "final_answer", "message": "ok", "reason": "enough"}'])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert state.execution_strategy == "react"
    assert state.plan is None
    assert state.response is not None
    assert state.response.message == "ok"
    assert "planner" not in adapter.requests[0].user_query


def test_explicit_plan_and_solve_executes_one_step_per_controller_turn() -> None:
    adapter = ScriptedChatAdapter(
        [
            _plan_json(
                [
                    _step_json("step_1", "echo_first", "echo"),
                    _step_json("step_2", "echo_second", "echo", depends_on=["step_1"]),
                ]
            ),
            '{"type": "execute_step", "step_id": "step_1", "tool_input": {"text": "first"}, "reason": "run first"}',
            '{"type": "execute_step", "step_id": "step_2", "tool_input": {"text": "second"}, "reason": "run second"}',
            '{"type": "final_answer", "message": "done", "reason": "both steps completed"}',
        ]
    )
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter, trace_store=trace_store)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="run two steps",
            execution_strategy="plan_and_solve",
        )
    )

    assert state.execution_strategy == "plan_and_solve"
    assert state.plan is not None
    assert state.plan_status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["echo", "echo"]
    assert [call.input for call in state.tool_calls] == [{"text": "first"}, {"text": "second"}]
    assert state.response is not None
    assert state.response.data["final_answer_source"] == "plan_and_solve"
    assert adapter.calls == 4
    assert "planner" in adapter.requests[0].user_query
    assert "plan-and-solve controller" in adapter.requests[1].user_query
    assert "plan-and-solve controller" in adapter.requests[2].user_query
    assert "plan_controller" in trace_store.node_path(state.run_id)
    assert "execute_plan_step" in trace_store.node_path(state.run_id)


def test_plan_and_solve_rejects_unknown_tool_plan() -> None:
    adapter = ScriptedChatAdapter([_plan_json([_step_json("step_1", "do_unknown", "unknown_tool")])])
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="use unknown", execution_strategy="plan_and_solve")
    )

    assert state.plan_status == "failed"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["plan_validation"]["code"] == "unknown_tool"


def test_plan_and_solve_rejects_unsatisfied_dependency_without_tool_execution() -> None:
    adapter = ScriptedChatAdapter(
        [
            _plan_json(
                [
                    _step_json("step_1", "echo_first", "echo"),
                    _step_json("step_2", "echo_second", "echo", depends_on=["step_1"]),
                ]
            ),
            '{"type": "execute_step", "step_id": "step_2", "tool_input": {"text": "second"}, "reason": "too early"}',
            '{"type": "final_answer", "message": "stopped after dependency rejection", "reason": "cannot proceed"}',
        ]
    )
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="run two steps", execution_strategy="plan_and_solve")
    )

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "stopped after dependency rejection"
    assert any(step.get("error_code") == "dependency_not_satisfied" for step in state.request.metadata["assistant_loop_steps"])


def test_plan_and_solve_can_replan_after_tool_failure() -> None:
    adapter = ScriptedChatAdapter(
        [
            _plan_json([_step_json("step_1", "search", "unstable_search")]),
            '{"type": "execute_step", "step_id": "step_1", "tool_input": {"query": "x"}, "reason": "try search"}',
            '{"type": "replan", "reason": "search failed"}',
            _plan_json([_step_json("step_1", "fallback_echo", "echo")]),
            '{"type": "final_answer", "message": "replanned after failure", "reason": "fallback is enough"}',
        ]
    )
    runtime = AgentGraphRuntime(registry=_registry(AlwaysFailTool(), EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="recover from failure", execution_strategy="plan_and_solve")
    )

    assert [call.tool_name for call in state.tool_calls] == ["unstable_search"]
    assert state.tool_results[0].success is False
    assert state.status == "completed"
    assert state.plan_revision_count == 1
    assert state.plan_status == "completed"
    assert state.response is not None
    assert state.response.message == "replanned after failure"


def test_plan_validator_rejects_cycles_and_step_limits() -> None:
    registry = _registry(EchoTool())
    cycle = TaskPlan(
        goal="cycle",
        steps=[
            TaskStep(step_id="step_1", action="a", tool_name="echo", depends_on=["step_2"]),
            TaskStep(step_id="step_2", action="b", tool_name="echo", depends_on=["step_1"]),
        ],
    )
    too_large = TaskPlan(
        goal="large",
        steps=[
            TaskStep(step_id=f"step_{index}", action="a", tool_name="echo")
            for index in range(1, 4)
        ],
    )

    assert PlanValidator().validate(cycle, registry).code == "cyclic_dependency"
    assert PlanValidator(max_steps=2).validate(too_large, registry).code == "plan_too_large"


def _registry(*tools: MockTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _plan_json(steps: list[dict[str, object]]) -> str:
    import json

    return json.dumps({"goal": "test goal", "steps": steps}, ensure_ascii=False)


def _step_json(
    step_id: str,
    action: str,
    tool_name: str,
    *,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    deps = list(depends_on or [])
    return {
        "step_id": step_id,
        "action": action,
        "tool_name": tool_name,
        "input_refs": deps,
        "depends_on": deps,
        "required_inputs": ["text"],
        "optional": False,
        "reason": action,
    }
