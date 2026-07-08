# Observer Hook Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an observer-only HookManager and adapters for existing event and trace observation flows.

**Architecture:** Implement a small `assistant_agent.services.hooks` module with `HookManager`, `HookEventSink`, and `HookTraceStore`. The manager dispatches existing `AgentEvent`, `TraceEvent`, and `HookDispatchError` records to observers without adding interception or changing runtime behavior.

**Tech Stack:** Python protocols, existing `AgentEvent`, existing `TraceEvent`, existing `HookDispatchError`, pytest.

## Global Constraints

- Add observer-only `HookManager`, `HookEventSink`, and `HookTraceStore`.
- Do not add intercepting hooks.
- Do not add `before_tool`, `after_tool`, `before_llm`, `after_llm`, or policy mutation hooks.
- Do not change `AgentGraphRuntime`, `ToolExecutor`, memory, provider, or Gateway behavior.
- Do not add plugin discovery, config loading, metrics exporters, audit exporters, OpenTelemetry, or UI.
- Do not allow observers to block or alter tool execution.
- Do not add dependencies.

---

## File Structure

- Create `src/assistant_agent/services/hooks.py`
  - Owns `HookObserver`, `HookManager`, `HookEventSink`, and `HookTraceStore`.
- Create `tests/test_hook_manager.py`
  - Covers observer dispatch, errors, adapters, and composition with Phase 1 sinks/stores.
- Modify `docs/observability-harness.md`
  - Documents the observer-only manager as Phase 2, explicitly not an interception layer.

---

### Task 1: HookManager Core

**Files:**
- Create: `src/assistant_agent/services/hooks.py`
- Create: `tests/test_hook_manager.py`

**Interfaces:**
- Produces: `HookObserver` protocol with optional `on_run_event`, `on_trace_event`, and `on_hook_error` methods.
- Produces: `HookManager(observers: Iterable[object] = (), *, continue_on_error: bool = True)`.
- Produces: `HookManager.add_observer(observer: object) -> None`.
- Produces: `HookManager.on_run_event(event: AgentEvent) -> None`.
- Produces: `HookManager.on_trace_event(event: TraceEvent) -> None`.
- Produces: `HookManager.on_hook_error(error: HookDispatchError) -> None`.
- Produces: `HookManager.errors -> list[HookDispatchError]`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_hook_manager.py`:

```python
import pytest

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.hook_dispatch import HookDispatchError
from assistant_agent.services.hooks import HookManager
from assistant_agent.services.trace_store import TraceEvent


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.hooks'`.

- [ ] **Step 3: Implement HookManager**

Create `src/assistant_agent/services/hooks.py`:

```python
"""Observer-only harness hooks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.hook_dispatch import HookDispatchError, build_hook_dispatch_error
from assistant_agent.services.trace_store import TraceEvent


class HookObserver(Protocol):
    """Observer protocol for harness lifecycle events."""

    def on_run_event(self, event: AgentEvent) -> None: ...

    def on_trace_event(self, event: TraceEvent) -> None: ...

    def on_hook_error(self, error: HookDispatchError) -> None: ...


class HookManager:
    """Dispatch observer-only hook events without changing runtime behavior."""

    def __init__(
        self,
        observers: Iterable[object] = (),
        *,
        continue_on_error: bool = True,
    ) -> None:
        self.observers = list(observers)
        self.continue_on_error = continue_on_error
        self._errors: list[HookDispatchError] = []

    @property
    def errors(self) -> list[HookDispatchError]:
        return list(self._errors)

    def add_observer(self, observer: object) -> None:
        self.observers.append(observer)

    def on_run_event(self, event: AgentEvent) -> None:
        self._dispatch("on_run_event", event)

    def on_trace_event(self, event: TraceEvent) -> None:
        self._dispatch("on_trace_event", event)

    def on_hook_error(self, error: HookDispatchError) -> None:
        self._dispatch_hook_error(error)

    def _dispatch(self, method_name: str, event: AgentEvent | TraceEvent) -> None:
        for index, observer in enumerate(self.observers):
            method = getattr(observer, method_name, None)
            if method is None:
                continue
            try:
                method(event)
            except Exception as exc:
                error = build_hook_dispatch_error(
                    target=observer,
                    target_index=index,
                    operation=method_name,
                    event=event,
                    exc=exc,
                )
                self._errors.append(error)
                self._dispatch_hook_error(error)
                if not self.continue_on_error:
                    raise

    def _dispatch_hook_error(self, error: HookDispatchError) -> None:
        for index, observer in enumerate(self.observers):
            method = getattr(observer, "on_hook_error", None)
            if method is None:
                continue
            try:
                method(error)
            except Exception as exc:
                self._errors.append(
                    build_hook_dispatch_error(
                        target=observer,
                        target_index=index,
                        operation="on_hook_error",
                        event=None,
                        exc=exc,
                    )
                )
                if not self.continue_on_error:
                    raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py -q
```

Expected: PASS.

---

### Task 2: Hook Adapters And Composition

**Files:**
- Modify: `src/assistant_agent/services/hooks.py`
- Modify: `tests/test_hook_manager.py`

**Interfaces:**
- Consumes: `HookManager`.
- Produces: `HookEventSink(manager: HookManager)`.
- Produces: `HookTraceStore(manager: HookManager)`.

- [ ] **Step 1: Add failing adapter tests**

Append to `tests/test_hook_manager.py`:

```python
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import CompositeEventSink, ListEventSink
from assistant_agent.services.hooks import HookEventSink, HookTraceStore
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore


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
```

- [ ] **Step 2: Run adapter tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py::test_hook_event_sink_forwards_to_manager tests/test_hook_manager.py::test_hook_trace_store_forwards_to_manager_and_reads_empty tests/test_hook_manager.py::test_runtime_can_compose_hook_event_sink_without_losing_existing_events tests/test_hook_manager.py::test_composite_trace_store_with_hook_trace_store_preserves_primary_reads -q
```

Expected: FAIL with `ImportError` for `HookEventSink` or `HookTraceStore`.

- [ ] **Step 3: Implement adapters**

Append to `src/assistant_agent/services/hooks.py`:

```python
class HookEventSink:
    """EventSink adapter that forwards AgentEvent records to HookManager."""

    def __init__(self, manager: HookManager) -> None:
        self.manager = manager

    def emit(self, event: AgentEvent) -> None:
        self.manager.on_run_event(event)


class HookTraceStore:
    """TraceStore adapter that forwards trace writes to HookManager."""

    def __init__(self, manager: HookManager) -> None:
        self.manager = manager

    def append(self, event: TraceEvent) -> None:
        self.manager.on_trace_event(event)

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        return []

    def list_by_trace(self, trace_id: str) -> list[TraceEvent]:
        return []

    def node_path(self, run_id: str) -> list[str]:
        return []

    def list_by_user(self, user_id: str) -> list[TraceEvent]:
        return []

    def delete_by_user(self, user_id: str) -> int:
        return 0
```

- [ ] **Step 4: Run adapter tests to verify they pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py -q
```

Expected: PASS.

---

### Task 3: Documentation And Verification

**Files:**
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Documents `HookManager`, `HookEventSink`, and `HookTraceStore` as observer-only harness primitives.

- [ ] **Step 1: Update docs**

Add after the existing paragraph about `CompositeEventSink` / `CompositeTraceStore`:

```markdown
`HookManager` builds on those composition primitives as an observer-only
vocabulary layer. `HookEventSink` forwards `AgentEvent` records to observers,
and `HookTraceStore` forwards trace writes to observers when used as a secondary
store. Hook observers cannot intercept or mutate assistant behavior; they only
receive prompt-safe lifecycle records and hook dispatch errors.
```

- [ ] **Step 2: Run focused verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py tests/test_harness_composite_sinks.py tests/test_agent_events.py -q
```

Expected: PASS.

- [ ] **Step 3: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: PASS.

- [ ] **Step 4: Run diff hygiene**

Run:

```bash
git diff --check -- docs/observability-harness.md docs/superpowers/plans src/assistant_agent/services tests
```

Expected: no output and exit code 0.

- [ ] **Step 5: Confirm stop boundary**

Confirm the diff does not include metrics, audit, export, plugin discovery, UI, interception hooks, runtime behavior changes, or tool executor behavior changes.

---

## Completion Criteria

Phase 2 is complete when:

- `HookManager` dispatches observer-only run events, trace events, and hook errors.
- `HookEventSink` and `HookTraceStore` compose with Phase 1 primitives.
- Observer failures are prompt-safe and do not fail runtime by default.
- Focused tests and fast tests pass.
- Docs state that hooks are observer-only.

Stop after this phase. The next phase should design concrete observer adapters such as metrics, audit, or export, not interception.
