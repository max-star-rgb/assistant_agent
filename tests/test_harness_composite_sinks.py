import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import CompositeEventSink, ListEventSink
from assistant_agent.services.hook_dispatch import build_hook_dispatch_error
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


class FailingTarget:
    pass


def test_hook_dispatch_error_sanitizes_message_without_event_payload() -> None:
    event = AgentEvent(
        type="tool_started",
        session_id="s1",
        run_id="run_1",
        payload={"api_key": "sk-secret-value", "raw": "must not be copied"},
    )

    error = build_hook_dispatch_error(
        target=FailingTarget(),
        target_index=2,
        operation="emit",
        event=event,
        exc=RuntimeError("api_key=sk-secret-value failed at /home/user/private/file.txt"),
    )

    assert error.target_index == 2
    assert error.target_name == "FailingTarget"
    assert error.operation == "emit"
    assert error.event_type == "tool_started"
    assert error.canonical_event is None
    assert "sk-secret-value" not in error.message
    assert "/home/user/private" not in error.message
    assert "must not be copied" not in error.message


class RecordingSink:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.calls.append(self.name)
        self.events.append(event)


class FailingSink:
    def __init__(self, message: str = "api_key=sk-secret-value failed") -> None:
        self.message = message

    def emit(self, event: AgentEvent) -> None:
        raise RuntimeError(self.message)


def test_composite_event_sink_fans_out_in_order() -> None:
    calls: list[str] = []
    first = RecordingSink("first", calls)
    second = RecordingSink("second", calls)
    event = AgentEvent(type="task_started", session_id="s1", run_id="run_1")

    CompositeEventSink([first, second]).emit(event)

    assert calls == ["first", "second"]
    assert first.events == [event]
    assert second.events == [event]


def test_composite_event_sink_records_error_and_continues() -> None:
    calls: list[str] = []
    good = RecordingSink("good", calls)
    event = AgentEvent(type="tool_started", session_id="s1", run_id="run_1")
    sink = CompositeEventSink([FailingSink(), good])

    sink.emit(event)

    assert calls == ["good"]
    assert len(sink.errors) == 1
    assert sink.errors[0].target_name == "FailingSink"
    assert sink.errors[0].operation == "emit"
    assert sink.errors[0].event_type == "tool_started"
    assert "sk-secret-value" not in sink.errors[0].message


def test_composite_event_sink_can_fail_fast() -> None:
    event = AgentEvent(type="task_started", session_id="s1", run_id="run_1")
    sink = CompositeEventSink([FailingSink()], continue_on_error=False)

    with pytest.raises(RuntimeError):
        sink.emit(event)

    assert len(sink.errors) == 1


class FailingTraceStore(InMemoryTraceStore):
    def append(self, event: TraceEvent) -> None:
        raise RuntimeError("authorization=Bearer secret-token trace write failed")

    def delete_by_user(self, user_id: str) -> int:
        raise RuntimeError("token=sk-secret-value delete failed")


def _trace_event(run_id: str = "run_1", trace_id: str = "trace_1") -> TraceEvent:
    return TraceEvent(
        trace_id=trace_id,
        run_id=run_id,
        user_id="u1",
        session_id="s1",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.started",
    )


def test_composite_trace_store_appends_primary_then_secondaries() -> None:
    primary = InMemoryTraceStore()
    secondary = InMemoryTraceStore()
    store = CompositeTraceStore(primary, [secondary])
    event = _trace_event()

    store.append(event)

    assert [item.run_id for item in primary.events] == ["run_1"]
    assert [item.run_id for item in secondary.events] == ["run_1"]


def test_composite_trace_store_reads_only_from_primary() -> None:
    primary = InMemoryTraceStore()
    secondary = InMemoryTraceStore()
    primary.append(_trace_event(run_id="run_primary"))
    secondary.append(_trace_event(run_id="run_secondary"))
    store = CompositeTraceStore(primary, [secondary])

    assert [event.run_id for event in store.list_by_user("u1")] == ["run_primary"]
    assert store.list_by_run("run_secondary") == []
    assert store.node_path("run_secondary") == []


def test_composite_trace_store_records_secondary_append_error_and_continues() -> None:
    primary = InMemoryTraceStore()
    store = CompositeTraceStore(primary, [FailingTraceStore()])

    store.append(_trace_event())

    assert [event.run_id for event in primary.events] == ["run_1"]
    assert len(store.errors) == 1
    assert store.errors[0].target_name == "FailingTraceStore"
    assert store.errors[0].operation == "append"
    assert store.errors[0].event_type == "observability"
    assert store.errors[0].canonical_event == "run.started"
    assert "secret-token" not in store.errors[0].message


def test_composite_trace_store_can_fail_fast_on_append() -> None:
    store = CompositeTraceStore(InMemoryTraceStore(), [FailingTraceStore()], continue_on_error=False)

    with pytest.raises(RuntimeError):
        store.append(_trace_event())

    assert len(store.errors) == 1


def test_composite_trace_store_delete_returns_primary_count_and_records_secondary_error() -> None:
    primary = InMemoryTraceStore()
    primary.append(_trace_event())
    store = CompositeTraceStore(primary, [FailingTraceStore()])

    deleted = store.delete_by_user("u1")

    assert deleted == 1
    assert primary.list_by_user("u1") == []
    assert len(store.errors) == 1
    assert store.errors[0].operation == "delete_by_user"
    assert "sk-secret-value" not in store.errors[0].message


def test_runtime_can_use_composite_event_sink_without_losing_order() -> None:
    first = ListEventSink()
    second = ListEventSink()
    sink = CompositeEventSink([first, second])

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好")
    )

    assert state.status == "completed"
    assert [event.type for event in first.events] == [event.type for event in second.events]
    assert first.events[0].type == "task_started"
    assert first.events[-1].type == "final_response"
    assert sink.errors == []
