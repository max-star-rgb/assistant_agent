import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import CompositeEventSink, ListEventSink
from assistant_agent.services.hook_dispatch import HookDispatchError
from assistant_agent.services.hooks import HookEventSink, HookManager, HookTraceStore
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


class RunObserver:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.events: list[AgentEvent] = []

    def on_run_event(self, event: AgentEvent) -> None:
        self.calls.append(self.name)
        self.events.append(event)


class TraceObserver:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.events: list[TraceEvent] = []

    def on_trace_event(self, event: TraceEvent) -> None:
        self.calls.append(self.name)
        self.events.append(event)


class ErrorObserver:
    def __init__(self) -> None:
        self.errors: list[HookDispatchError] = []

    def on_hook_error(self, error: HookDispatchError) -> None:
        self.errors.append(error)


class FailingRunObserver:
    def on_run_event(self, event: AgentEvent) -> None:
        raise RuntimeError("api_key=sk-secret-value run observer failed")


class FailingErrorObserver:
    def on_hook_error(self, error: HookDispatchError) -> None:
        raise RuntimeError("token=sk-secret-value hook error observer failed")


class MissingMethodsObserver:
    pass


def _run_event() -> AgentEvent:
    return AgentEvent(type="task_started", session_id="s1", run_id="run_1")


def _trace_event() -> TraceEvent:
    return TraceEvent(
        trace_id="trace_1",
        run_id="run_1",
        user_id="u1",
        session_id="s1",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.started",
    )


def test_hook_manager_dispatches_run_events_in_order() -> None:
    calls: list[str] = []
    first = RunObserver("first", calls)
    second = RunObserver("second", calls)
    event = _run_event()

    HookManager([first, second]).on_run_event(event)

    assert calls == ["first", "second"]
    assert first.events == [event]
    assert second.events == [event]


def test_hook_manager_dispatches_trace_events_in_order() -> None:
    calls: list[str] = []
    first = TraceObserver("first", calls)
    second = TraceObserver("second", calls)
    event = _trace_event()

    HookManager([first, second]).on_trace_event(event)

    assert calls == ["first", "second"]
    assert first.events == [event]
    assert second.events == [event]


def test_hook_manager_ignores_missing_observer_methods() -> None:
    manager = HookManager([MissingMethodsObserver()])

    manager.on_run_event(_run_event())
    manager.on_trace_event(_trace_event())

    assert manager.errors == []


def test_hook_manager_records_error_and_notifies_error_observer() -> None:
    errors = ErrorObserver()
    manager = HookManager([FailingRunObserver(), errors])

    manager.on_run_event(_run_event())

    assert len(manager.errors) == 1
    assert errors.errors == manager.errors
    assert manager.errors[0].target_name == "FailingRunObserver"
    assert manager.errors[0].operation == "on_run_event"
    assert manager.errors[0].event_type == "task_started"
    assert "sk-secret-value" not in manager.errors[0].message


def test_hook_manager_does_not_recursively_dispatch_hook_error_failures() -> None:
    manager = HookManager([FailingRunObserver(), FailingErrorObserver()])

    manager.on_run_event(_run_event())

    assert len(manager.errors) == 2
    assert [error.operation for error in manager.errors] == ["on_run_event", "on_hook_error"]


def test_hook_manager_can_fail_fast_after_recording_error() -> None:
    manager = HookManager([FailingRunObserver()], continue_on_error=False)

    with pytest.raises(RuntimeError):
        manager.on_run_event(_run_event())

    assert len(manager.errors) == 1


def test_hook_event_sink_forwards_to_manager() -> None:
    calls: list[str] = []
    observer = RunObserver("observer", calls)
    manager = HookManager([observer])
    event = _run_event()

    HookEventSink(manager).emit(event)

    assert observer.events == [event]


def test_hook_trace_store_forwards_to_manager_and_reads_empty() -> None:
    calls: list[str] = []
    observer = TraceObserver("observer", calls)
    manager = HookManager([observer])
    store = HookTraceStore(manager)
    event = _trace_event()

    store.append(event)

    assert observer.events == [event]
    assert store.list_by_run("run_1") == []
    assert store.list_by_trace("trace_1") == []
    assert store.node_path("run_1") == []
    assert store.list_by_user("u1") == []
    assert store.delete_by_user("u1") == 0


def test_runtime_can_compose_hook_event_sink_without_losing_existing_events() -> None:
    list_sink = ListEventSink()
    hook_observer = RunObserver("hook", [])
    manager = HookManager([hook_observer])
    sink = CompositeEventSink([list_sink, HookEventSink(manager)])

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好")
    )

    assert state.status == "completed"
    assert [event.type for event in hook_observer.events] == [event.type for event in list_sink.events]
    assert manager.errors == []


def test_composite_trace_store_with_hook_trace_store_preserves_primary_reads() -> None:
    primary = InMemoryTraceStore()
    observer = TraceObserver("hook", [])
    manager = HookManager([observer])
    store = CompositeTraceStore(primary, [HookTraceStore(manager)])
    event = _trace_event()

    store.append(event)

    assert store.list_by_run("run_1") == primary.list_by_run("run_1")
    assert observer.events == [event]
