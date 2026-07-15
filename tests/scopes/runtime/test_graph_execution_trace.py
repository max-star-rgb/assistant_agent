from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.product_search_tool import ProductSearchTool
from assistant_agent.tools.registry import ToolRegistry


class SensitiveFailingProductSearchTool(ProductSearchTool):
    def _run(self, input, context) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="provider_timeout: bearer sk-test api_key=abc secret=hidden timed out",
        )


def test_trace_events_include_run_id_and_trace_id() -> None:
    trace_store = InMemoryTraceStore()

    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    events = trace_store.list_by_run(state.run_id)
    assert state.run_id.startswith("run_")
    assert state.trace_id.startswith("trace_")
    assert events
    assert {event.run_id for event in events} == {state.run_id}
    assert {event.trace_id for event in events} == {state.trace_id}


def test_trace_store_can_query_node_path() -> None:
    trace_store = InMemoryTraceStore()

    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    assert trace_store.node_path(state.run_id) == [
        "load_memory",
        "assistant",
        "execute_tool",
        "assistant",
        "compose_response",
        "save_memory",
    ]


def test_tool_failure_is_recorded_in_trace() -> None:
    trace_store = InMemoryTraceStore()
    state = _runtime_with_sensitive_failure(trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    failed_events = [event for event in trace_store.list_by_run(state.run_id) if event.event_type == "tool_failed"]

    assert state.status == "failed"
    assert len(failed_events) == 1
    assert failed_events[0].node_name == "execute_tool"
    assert failed_events[0].tool_name == "product_search"
    assert failed_events[0].error is not None
    assert failed_events[0].error["code"] == "provider_timeout"


def test_trace_does_not_include_sensitive_fields() -> None:
    trace_store = InMemoryTraceStore()
    state = _runtime_with_sensitive_failure(trace_store).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找相似款",
            metadata={"api_key": "should-not-appear"},
        )
    )

    dumped = "\n".join(event.model_dump_json() for event in trace_store.list_by_run(state.run_id)).lower()

    assert "sk-test" not in dumped
    assert "api_key=abc" not in dumped
    assert "hidden" not in dumped
    assert "should-not-appear" not in dumped
    assert "[redacted]" in dumped


def _runtime_with_sensitive_failure(trace_store: InMemoryTraceStore) -> AgentGraphRuntime:
    registry = ToolRegistry()
    registry.register(SensitiveFailingProductSearchTool())
    return AgentGraphRuntime(registry=registry, trace_store=trace_store)
