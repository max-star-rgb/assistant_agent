from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.hook_invariants import TraceInvariantObserver
from assistant_agent.services.trace_store import InMemoryTraceStore, trace_debug_summary


class ScriptedNativeChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


def test_native_validation_rejection_does_not_enter_tool_executor_and_is_observable() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-rejection-test",
                tool_calls=[
                    NativeToolCall(
                        id="call_rejected_1",
                        name="product_search",
                        arguments={},
                    )
                ],
                message_kind="tool_calls",
            )
        ]
    )
    runtime = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找鞋"))

    raw_events = trace_store.list_by_run(state.run_id)
    events = trace_debug_summary(raw_events)["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]
    validation = next(event for event in events if event["canonical_event"] == "action.validation.finished")
    observation = next(event for event in events if event["canonical_event"] == "tool.observation")

    assert state.tool_calls == []
    assert validation["status"] == "rejected"
    assert "tool.started" not in canonical
    assert "tool.finished" not in canonical
    assert "tool.failed" not in canonical
    assert observation["status"] == "rejected"
    assert observation["tool_name"] == "product_search"
    assert observation["error_code"] == "invalid_tool_input"
    assert TraceInvariantObserver(raw_events).is_valid() is True
