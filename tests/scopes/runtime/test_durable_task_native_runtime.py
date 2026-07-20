from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.registry import create_default_registry


class ScriptedNativeAdapter:
    provider = "scripted-native"

    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.calls += 1
        self.requests.append(request)
        return self.result


def test_explicit_durable_mode_fails_closed_before_llm_when_disabled() -> None:
    adapter = ScriptedNativeAdapter(_final("should not run"))
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="稍后完成",
            task_execution_mode="durable",
        )
    )

    assert adapter.calls == 0
    assert state.status == "failed"
    assert state.response is not None
    assert state.response.data["errors"][0]["code"] == "durable_tasks_disabled"


def test_durable_plan_submission_is_terminal_without_second_llm_call() -> None:
    runtime, adapter, service = _durable_runtime(_native("task_plan_submit", _plan_input()))

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="建立任务",
            task_execution_mode="durable",
        )
    )

    assert adapter.calls == 1
    assert [call.tool_name for call in state.tool_calls] == ["task_plan_submit"]
    assert state.response is not None
    assert state.response.data["task"]["submission_status"] == "accepted"
    assert service.claim_next(worker_id="probe") is not None


def test_mixed_plan_and_business_tool_batch_executes_neither() -> None:
    result = _native_batch(
        [
            ("task_plan_submit", _plan_input()),
            ("shopping_search", {"query": "耳机"}),
        ]
    )
    runtime, adapter, service = _durable_runtime(result)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="建立任务并搜索",
            task_execution_mode="durable",
        )
    )

    assert adapter.calls == 1
    assert state.tool_calls == []
    assert service.claim_next(worker_id="probe") is None
    assert state.response.data["errors"][0]["code"] == "durable_plan_must_be_standalone"


def test_durable_mode_rejects_direct_business_tool_call() -> None:
    runtime, adapter, _ = _durable_runtime(
        _native("shopping_search", {"query": "耳机", "limit": 2})
    )

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="异步搜索",
            task_execution_mode="durable",
        )
    )

    assert adapter.calls == 1
    assert state.tool_calls == []
    assert state.response.data["validator_result"]["code"] == "durable_plan_required"


def test_foreground_mode_does_not_expose_task_plan_tool() -> None:
    runtime, adapter, _ = _durable_runtime(_final("立即回答"))

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="立即回答",
            task_execution_mode="foreground",
        )
    )

    names = {tool["function"]["name"] for tool in adapter.requests[0].tools}
    assert "task_plan_submit" not in names
    assert state.response.message == "立即回答"


def _durable_runtime(result: ChatResult):
    config = ProviderConfig(durable_tasks_enabled=True)
    registry = create_default_registry(ProviderConfig())
    service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    adapter = ScriptedNativeAdapter(result)
    runtime = AgentGraphRuntime(
        registry=registry,
        config=config,
        chat_adapter=adapter,
        durable_task_service=service,
    )
    return runtime, adapter, service


def _plan_input() -> dict:
    plan = TaskPlan(
        goal="搜索耳机",
        steps=[TaskStep(step_id="step_1", action="搜索", tool_name="shopping_search")],
    )
    return {"plan": plan.model_dump(mode="json"), "revision_reason": "initial"}


def _native(name: str, arguments: dict) -> ChatResult:
    return _native_batch([(name, arguments)])


def _native_batch(calls: list[tuple[str, dict]]) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[
            NativeToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls, start=1)
        ],
        finish_reason="tool_calls",
        message_kind="tool_call",
        provider="scripted-native",
        model="native-test",
    )


def _final(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="native-test",
    )
