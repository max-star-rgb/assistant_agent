import json

from assistant_agent.services.hook_dispatch import HookDispatchError
from assistant_agent.services.hook_invariants import TraceInvariantObserver
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


def _event(
    canonical_event: str,
    *,
    status: str | None = None,
    run_id: str = "run_1",
    trace_id: str = "trace_1",
    node_name: str = "runtime",
    tool_name: str | None = None,
    attributes: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id=trace_id,
        run_id=run_id,
        user_id="u1",
        session_id="s1",
        node_name=node_name,
        event_type="observability",
        canonical_event=canonical_event,
        status=status,
        tool_name=tool_name,
        attributes=attributes or {},
        error=error,
    )


def _tool_event(
    canonical_event: str,
    *,
    status: str,
    error: dict[str, object] | None = None,
) -> TraceEvent:
    return _event(
        canonical_event,
        status=status,
        node_name="tool_executor",
        tool_name="product_search",
        attributes={"tool_call_id": "call_1", "step_id": "step_1"},
        error=error,
    )


def test_trace_invariant_observer_accepts_valid_run_and_tool_lifecycle() -> None:
    observer = TraceInvariantObserver()
    for event in [
        _event("run.started", status="started"),
        _tool_event("tool.started", status="started"),
        _tool_event("tool.finished", status="succeeded"),
        _tool_event("tool.observation", status="succeeded"),
        _event("run.completed", status="completed"),
    ]:
        observer.on_trace_event(event)

    assert observer.violations() == []
    assert observer.is_valid() is True


def test_trace_invariant_observer_reports_run_without_terminal_event() -> None:
    observer = TraceInvariantObserver([_event("run.started", status="started")])

    violations = observer.violations()

    assert [violation.code for violation in violations] == ["missing_run_terminal"]
    assert violations[0].run_id == "run_1"
    assert violations[0].canonical_event == "run.started"


def test_trace_invariant_observer_reports_tool_without_terminal_event() -> None:
    observer = TraceInvariantObserver(
        [
            _event("run.started", status="started"),
            _tool_event("tool.started", status="started"),
            _event("run.completed", status="completed"),
        ]
    )

    codes = [violation.code for violation in observer.violations()]

    assert codes == ["missing_tool_terminal"]


def test_trace_invariant_observer_reports_observation_without_prior_tool_or_rejection() -> None:
    observer = TraceInvariantObserver(
        [
            _event("run.started", status="started"),
            _tool_event("tool.observation", status="failed"),
            _event("run.failed", status="failed"),
        ]
    )

    violations = observer.violations()

    assert [violation.code for violation in violations] == ["tool_observation_without_prior_action"]
    assert violations[0].tool_name == "product_search"


def test_trace_invariant_observer_allows_observation_after_validation_rejection() -> None:
    observer = TraceInvariantObserver(
        [
            _event("run.started", status="started"),
            _event("action.validation.finished", status="rejected"),
            _tool_event("tool.observation", status="failed"),
            _event("run.failed", status="failed"),
        ]
    )

    assert observer.violations() == []


def test_trace_invariant_observer_reports_failed_tool_missing_error_detail() -> None:
    observer = TraceInvariantObserver(
        [
            _event("run.started", status="started"),
            _tool_event("tool.started", status="started"),
            _tool_event("tool.failed", status="failed", error={"message": "timed out"}),
            _event("run.failed", status="failed"),
        ]
    )

    codes = [violation.code for violation in observer.violations()]

    assert codes == ["missing_tool_error_code", "missing_tool_recovery_action"]


def test_trace_invariant_observer_redacts_events_and_reports_unredacted_hook_errors() -> None:
    observer = TraceInvariantObserver()
    observer.on_trace_event(_event("run.started", status="started", attributes={"api_key": "sk-secret-value"}))
    observer.on_trace_event(_event("run.completed", status="completed"))
    observer.on_hook_error(
        HookDispatchError(
            target_index=0,
            target_name="FailingObserver",
            operation="on_trace_event",
            event_type="observability",
            canonical_event="run.started",
            message="api_key=sk-secret-value failed",
        )
    )

    violations = observer.violations()
    dumped_events = json.dumps([event.model_dump(mode="json") for event in observer.events])
    dumped_violations = json.dumps([violation.__dict__ for violation in violations])

    assert "sk-secret-value" not in dumped_events
    assert [violation.code for violation in violations] == ["hook_error_not_redacted"]
    assert "sk-secret-value" not in dumped_violations


def test_trace_invariant_observer_clear_resets_local_state() -> None:
    observer = TraceInvariantObserver([_event("run.started", status="started")])
    observer.on_hook_error(
        HookDispatchError(
            target_index=0,
            target_name="FailingObserver",
            operation="on_trace_event",
            event_type="observability",
            canonical_event="run.started",
            message="redacted failure",
        )
    )

    observer.clear()

    assert observer.events == []
    assert observer.hook_errors == []
    assert observer.violations() == []


def test_trace_invariant_observer_composes_through_hook_trace_store_without_changing_primary_reads() -> None:
    primary = InMemoryTraceStore()
    observer = TraceInvariantObserver()
    manager = HookManager([observer])
    store = CompositeTraceStore(primary, [HookTraceStore(manager)])

    store.append(_event("run.started", status="started"))
    store.append(_event("run.completed", status="completed"))

    assert store.list_by_run("run_1") == primary.list_by_run("run_1")
    assert observer.is_valid() is True
    assert manager.errors == []
