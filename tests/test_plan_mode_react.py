import json

from pydantic import BaseModel

from assistant_agent.agent.plan_validator import PlanValidator
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


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
    description = "Echo text for plan-mode tests."
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text}, output_ref=f"echo://{input.text}")


class FailingInput(BaseModel):
    query: str


class AlwaysFailTool(MockTool):
    name = "unstable_search"
    description = "Always fails for plan revision tests."
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
    assert "plan-and-solve controller" not in adapter.requests[0].user_query


def test_plan_mode_executes_one_tool_call_per_assistant_turn() -> None:
    adapter = ScriptedChatAdapter(
        [
            _enter_plan_json(
                [
                    _step_json("step_1", "echo_first", "echo"),
                    _step_json("step_2", "echo_second", "echo", depends_on=["step_1"]),
                ]
            ),
            _tool_call_json("step_1", "echo", {"text": "first"}),
            _tool_call_json("step_2", "echo", {"text": "second"}),
            _exit_plan_json("done"),
        ]
    )
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter, trace_store=trace_store)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="run two steps"))

    assert state.execution_strategy == "react"
    assert state.plan is not None
    assert state.plan_status == "completed"
    assert state.current_step_id is None
    assert [call.tool_name for call in state.tool_calls] == ["echo", "echo"]
    assert [call.input for call in state.tool_calls] == [{"text": "first"}, {"text": "second"}]
    assert state.response is not None
    assert state.response.message == "done"
    assert state.response.data["final_answer_source"] == "assistant_loop"
    assert state.response.data["plan_status"] == "completed"
    assert adapter.calls == 4
    assert "plan-and-solve controller" not in " ".join(request.user_query for request in adapter.requests)
    assert "apply_plan_mode_transition" in trace_store.node_path(state.run_id)
    assert "execute_tool" in trace_store.node_path(state.run_id)


def test_plan_mode_rejects_unknown_tool_plan() -> None:
    adapter = ScriptedChatAdapter([_enter_plan_json([_step_json("step_1", "do_unknown", "unknown_tool")])])
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use unknown"))

    assert state.plan_status == "failed"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["plan_validation"]["code"] == "unknown_tool"


def test_plan_mode_rejects_unsatisfied_dependency_without_tool_execution() -> None:
    adapter = ScriptedChatAdapter(
        [
            _enter_plan_json(
                [
                    _step_json("step_1", "echo_first", "echo"),
                    _step_json("step_2", "echo_second", "echo", depends_on=["step_1"]),
                ]
            ),
            _tool_call_json("step_2", "echo", {"text": "second"}),
            '{"type": "final_answer", "message": "stopped after dependency rejection", "reason": "cannot proceed"}',
        ]
    )
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="run two steps"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "stopped after dependency rejection"
    assert any(step.get("error_code") == "dependency_not_satisfied" for step in state.request.metadata["assistant_loop_steps"])
    assert any(error.details.get("code") == "dependency_not_satisfied" for error in state.errors)


def test_plan_mode_can_revise_after_tool_failure() -> None:
    adapter = ScriptedChatAdapter(
        [
            _enter_plan_json([_step_json("step_1", "search", "unstable_search", required_inputs=["query"])]),
            _tool_call_json("step_1", "unstable_search", {"query": "x"}),
            _enter_plan_json([_step_json("step_1", "fallback_echo", "echo")], reason="search failed; revise plan"),
            _tool_call_json("step_1", "echo", {"text": "fallback"}),
            _exit_plan_json("revised after failure"),
        ]
    )
    runtime = AgentGraphRuntime(registry=_registry(AlwaysFailTool(), EchoTool()), chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="recover from failure"))

    assert [call.tool_name for call in state.tool_calls] == ["unstable_search", "echo"]
    assert state.tool_results[0].success is False
    assert state.status == "completed"
    assert state.plan_revision_count == 1
    assert state.plan_status == "completed"
    assert state.response is not None
    assert state.response.message == "revised after failure"


def test_legacy_plan_and_solve_request_uses_assistant_loop_plan_mode() -> None:
    adapter = ScriptedChatAdapter(
        [
            _enter_plan_json([_step_json("step_1", "echo_first", "echo")]),
            _tool_call_json("step_1", "echo", {"text": "legacy"}),
            _exit_plan_json("legacy done"),
        ]
    )
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(registry=_registry(EchoTool()), chat_adapter=adapter, trace_store=trace_store)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="legacy strategy", execution_strategy="plan_and_solve")
    )

    assert state.execution_strategy == "plan_and_solve"
    assert state.response is not None
    assert state.response.message == "legacy done"
    assert "plan_controller" not in trace_store.node_path(state.run_id)
    assert "apply_plan_mode_transition" in trace_store.node_path(state.run_id)
    assert any("调用方计划模式提示" in message["content"] for message in adapter.requests[0].messages)


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


def _enter_plan_json(steps: list[dict[str, object]], *, reason: str = "need a plan") -> str:
    return json.dumps(
        {
            "type": "enter_plan_mode",
            "plan": {"goal": "test goal", "steps": steps},
            "reason": reason,
        },
        ensure_ascii=False,
    )


def _exit_plan_json(message: str) -> str:
    return json.dumps(
        {
            "type": "exit_plan_mode",
            "next_action": "final_answer",
            "message": message,
            "reason": "plan complete",
        },
        ensure_ascii=False,
    )


def _tool_call_json(step_id: str, tool_name: str, tool_input: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "step_id": step_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": f"run {step_id}",
        },
        ensure_ascii=False,
    )


def _step_json(
    step_id: str,
    action: str,
    tool_name: str,
    *,
    depends_on: list[str] | None = None,
    required_inputs: list[str] | None = None,
) -> dict[str, object]:
    deps = list(depends_on or [])
    return {
        "step_id": step_id,
        "action": action,
        "tool_name": tool_name,
        "input_refs": deps,
        "depends_on": deps,
        "required_inputs": list(required_inputs or ["text"]),
        "optional": False,
        "reason": action,
    }
