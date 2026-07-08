from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.hook_invariants import TraceInvariantObserver
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent


class ScriptedNativeChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


def _assert_trace_invariants(trace_store: InMemoryTraceStore, run_id: str) -> None:
    observer = TraceInvariantObserver(trace_store.list_by_run(run_id))
    violations = observer.violations()
    assert violations == []


def test_mock_runtime_trace_satisfies_phase0_invariant_gate() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找相似款"))

    _assert_trace_invariants(trace_store, state.run_id)


def test_native_runtime_trace_satisfies_phase0_invariant_gate() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-phase0-test",
                tool_calls=[
                    NativeToolCall(
                        id="call_native_1",
                        name="product_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                response_text="找到了一些白色运动鞋。",
                provider="scripted",
                model="native-phase0-test",
                message_kind="content",
            ),
        ]
    )
    runtime = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋"))

    _assert_trace_invariants(trace_store, state.run_id)


def test_phase0_invariant_gate_reports_broken_trace() -> None:
    broken = TraceEvent(
        trace_id="trace_broken",
        run_id="run_broken",
        user_id="u1",
        session_id="s1",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.started",
        status="started",
    )

    violations = TraceInvariantObserver([broken]).violations()

    assert [violation.code for violation in violations] == ["missing_run_terminal"]
