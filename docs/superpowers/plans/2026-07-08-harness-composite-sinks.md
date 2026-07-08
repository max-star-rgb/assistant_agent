# Harness Composite Sinks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ordered, failure-isolated composite event and trace dispatch primitives for the existing observability harness.

**Architecture:** Add a tiny shared diagnostics helper, then extend the existing `EventSink` and `TraceStore` protocol modules with composite implementations. Writes fan out in registration order; trace reads remain primary-store only.

**Tech Stack:** Python, dataclasses, existing `AgentEvent`, existing `TraceEvent`, existing `sanitize_error_message`, pytest.

## Global Constraints

- Keep this phase to `CompositeEventSink`, `CompositeTraceStore`, and prompt-safe dispatch diagnostics.
- Do not add a generic `HookManager`.
- Do not add intercepting hooks or change assistant decisions, tool selection, memory policy, provider behavior, Gateway behavior, or tool governance.
- Do not allow any new path to bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not merge trace reads across stores.
- Do not add dependencies.
- Preserve mock/local/offline defaults.
- Do not edit unrelated dirty worktree files.

---

## File Structure

- Create `src/assistant_agent/services/hook_dispatch.py`
  - Owns `HookDispatchError` and one helper to build prompt-safe dispatch diagnostics.
- Modify `src/assistant_agent/services/event_sink.py`
  - Adds `CompositeEventSink`.
- Modify `src/assistant_agent/services/trace_store.py`
  - Adds `CompositeTraceStore`.
- Create `tests/test_harness_composite_sinks.py`
  - Covers fan-out order, failure isolation, fail-fast mode, read delegation, delete behavior, sanitization, and runtime integration.

---

### Task 1: Shared Dispatch Diagnostics

**Files:**
- Create: `src/assistant_agent/services/hook_dispatch.py`
- Test: `tests/test_harness_composite_sinks.py`

**Interfaces:**
- Produces: `HookDispatchError` dataclass.
- Produces: `build_hook_dispatch_error(target: object, target_index: int, operation: str, event: object | None, exc: BaseException) -> HookDispatchError`.

- [ ] **Step 1: Write the failing tests**

Add this to `tests/test_harness_composite_sinks.py`:

```python
import pytest

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.hook_dispatch import build_hook_dispatch_error


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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_hook_dispatch_error_sanitizes_message_without_event_payload -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.hook_dispatch'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/assistant_agent/services/hook_dispatch.py`:

```python
"""Prompt-safe diagnostics for harness hook dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.services.provider_errors import sanitize_error_message


@dataclass(frozen=True)
class HookDispatchError:
    """Prompt-safe record of a failed event or trace dispatch target."""

    target_index: int
    target_name: str
    operation: str
    event_type: str | None
    canonical_event: str | None
    message: str


def build_hook_dispatch_error(
    *,
    target: object,
    target_index: int,
    operation: str,
    event: object | None,
    exc: BaseException,
) -> HookDispatchError:
    """Build a diagnostic without copying raw event payloads."""

    return HookDispatchError(
        target_index=target_index,
        target_name=type(target).__name__,
        operation=operation,
        event_type=_safe_text(_event_type(event)),
        canonical_event=_safe_text(getattr(event, "canonical_event", None)),
        message=sanitize_error_message(exc),
    )


def _event_type(event: object | None) -> Any:
    if event is None:
        return None
    return getattr(event, "type", None) or getattr(event, "event_type", None)


def _safe_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_hook_dispatch_error_sanitizes_message_without_event_payload -q
```

Expected: PASS.

---

### Task 2: Composite Event Sink

**Files:**
- Modify: `src/assistant_agent/services/event_sink.py`
- Test: `tests/test_harness_composite_sinks.py`

**Interfaces:**
- Consumes: `HookDispatchError`, `build_hook_dispatch_error`.
- Produces: `CompositeEventSink(sinks: Iterable[EventSink], *, continue_on_error: bool = True)`.
- Produces: `CompositeEventSink.errors -> list[HookDispatchError]`.

- [ ] **Step 1: Write the failing tests**

Append these tests to `tests/test_harness_composite_sinks.py`:

```python
from assistant_agent.services.event_sink import CompositeEventSink


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_composite_event_sink_fans_out_in_order tests/test_harness_composite_sinks.py::test_composite_event_sink_records_error_and_continues tests/test_harness_composite_sinks.py::test_composite_event_sink_can_fail_fast -q
```

Expected: FAIL with `ImportError` for `CompositeEventSink`.

- [ ] **Step 3: Write minimal implementation**

Update `src/assistant_agent/services/event_sink.py`:

```python
"""Runtime event sink abstractions."""

from collections.abc import Iterable
from typing import Protocol

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.hook_dispatch import HookDispatchError, build_hook_dispatch_error


class EventSink(Protocol):
    """Event destination for runtime and tool lifecycle events."""

    def emit(self, event: AgentEvent) -> None:
        """Store or forward an event."""


class CompositeEventSink:
    """Fan out runtime events to multiple sinks with failure isolation."""

    def __init__(
        self,
        sinks: Iterable[EventSink],
        *,
        continue_on_error: bool = True,
    ) -> None:
        self.sinks = list(sinks)
        self.continue_on_error = continue_on_error
        self._errors: list[HookDispatchError] = []

    @property
    def errors(self) -> list[HookDispatchError]:
        return list(self._errors)

    def emit(self, event: AgentEvent) -> None:
        for index, sink in enumerate(self.sinks):
            try:
                sink.emit(event)
            except Exception as exc:
                self._errors.append(
                    build_hook_dispatch_error(
                        target=sink,
                        target_index=index,
                        operation="emit",
                        event=event,
                        exc=exc,
                    )
                )
                if not self.continue_on_error:
                    raise


class ListEventSink:
    """In-memory event sink for local runtime and WebSocket tests."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_composite_event_sink_fans_out_in_order tests/test_harness_composite_sinks.py::test_composite_event_sink_records_error_and_continues tests/test_harness_composite_sinks.py::test_composite_event_sink_can_fail_fast -q
```

Expected: PASS.

---

### Task 3: Composite Trace Store

**Files:**
- Modify: `src/assistant_agent/services/trace_store.py`
- Test: `tests/test_harness_composite_sinks.py`

**Interfaces:**
- Consumes: `HookDispatchError`, `build_hook_dispatch_error`.
- Produces: `CompositeTraceStore(primary: TraceStore, secondaries: Iterable[TraceStore] = (), *, continue_on_error: bool = True)`.
- Produces: `CompositeTraceStore.errors -> list[HookDispatchError]`.

- [ ] **Step 1: Write the failing tests**

Append these tests to `tests/test_harness_composite_sinks.py`:

```python
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_composite_trace_store_appends_primary_then_secondaries tests/test_harness_composite_sinks.py::test_composite_trace_store_reads_only_from_primary tests/test_harness_composite_sinks.py::test_composite_trace_store_records_secondary_append_error_and_continues tests/test_harness_composite_sinks.py::test_composite_trace_store_can_fail_fast_on_append tests/test_harness_composite_sinks.py::test_composite_trace_store_delete_returns_primary_count_and_records_secondary_error -q
```

Expected: FAIL with `ImportError` for `CompositeTraceStore`.

- [ ] **Step 3: Write minimal implementation**

Update imports in `src/assistant_agent/services/trace_store.py`:

```python
from collections.abc import Callable, Iterable
```

Add:

```python
from assistant_agent.services.hook_dispatch import HookDispatchError, build_hook_dispatch_error
```

Add `CompositeTraceStore` after `JsonlTraceStore`:

```python
class CompositeTraceStore:
    """Fan out trace writes while keeping reads deterministic from primary."""

    def __init__(
        self,
        primary: TraceStore,
        secondaries: Iterable[TraceStore] = (),
        *,
        continue_on_error: bool = True,
    ) -> None:
        self.primary = primary
        self.secondaries = list(secondaries)
        self.continue_on_error = continue_on_error
        self._errors: list[HookDispatchError] = []

    @property
    def errors(self) -> list[HookDispatchError]:
        return list(self._errors)

    def append(self, event: TraceEvent) -> None:
        for index, store in enumerate(self._stores()):
            try:
                store.append(event)
            except Exception as exc:
                self._record_error(
                    target=store,
                    target_index=index,
                    operation="append",
                    event=event,
                    exc=exc,
                )
                if not self.continue_on_error:
                    raise

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        return self.primary.list_by_run(run_id)

    def list_by_trace(self, trace_id: str) -> list[TraceEvent]:
        return self.primary.list_by_trace(trace_id)

    def node_path(self, run_id: str) -> list[str]:
        return self.primary.node_path(run_id)

    def list_by_user(self, user_id: str) -> list[TraceEvent]:
        return self.primary.list_by_user(user_id)

    def delete_by_user(self, user_id: str) -> int:
        deleted = 0
        for index, store in enumerate(self._stores()):
            try:
                result = store.delete_by_user(user_id)
                if index == 0:
                    deleted = result
            except Exception as exc:
                self._record_error(
                    target=store,
                    target_index=index,
                    operation="delete_by_user",
                    event=None,
                    exc=exc,
                )
                if not self.continue_on_error:
                    raise
        return deleted

    def _stores(self) -> list[TraceStore]:
        return [self.primary, *self.secondaries]

    def _record_error(
        self,
        *,
        target: object,
        target_index: int,
        operation: str,
        event: TraceEvent | None,
        exc: BaseException,
    ) -> None:
        self._errors.append(
            build_hook_dispatch_error(
                target=target,
                target_index=target_index,
                operation=operation,
                event=event,
                exc=exc,
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py -q
```

Expected: PASS.

---

### Task 4: Runtime Integration Smoke

**Files:**
- Test: `tests/test_harness_composite_sinks.py`

**Interfaces:**
- Consumes: `CompositeEventSink`.
- Consumes: `AgentGraphRuntime(event_sink=...)`.

- [ ] **Step 1: Write the failing integration test**

Append this test:

```python
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import ListEventSink


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
```

- [ ] **Step 2: Run test to verify it passes with implementation**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py::test_runtime_can_use_composite_event_sink_without_losing_order -q
```

Expected: PASS.

If it fails because `CompositeEventSink` breaks `_ResponseDeltaTrackingEventSink`, fix `CompositeEventSink` only; do not modify runtime behavior.

---

### Task 5: Documentation And Focused Verification

**Files:**
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Documents the new Phase 1 composition primitives and stop boundary.

- [ ] **Step 1: Update observability harness docs**

Add one paragraph under `Current Surfaces` after the paragraph ending `live runtime lifecycle events.`:

```markdown
For local composition, `CompositeEventSink` can fan out one runtime event stream
to several sinks, and `CompositeTraceStore` can fan out trace writes to a
primary store plus secondary stores while keeping reads primary-only. These are
observer composition primitives, not a generic HookManager and not interception
points for changing assistant behavior.
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py tests/test_agent_events.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 3: Run diff hygiene**

Run:

```bash
git diff --check -- docs/observability-harness.md docs/superpowers/plans src/assistant_agent/services tests/test_harness_composite_sinks.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Review scope**

Confirm the diff does not include:

- generic `HookManager`
- intercepting hook methods
- changes to `ToolExecutor.run_tool()` behavior
- changes to `AgentGraphRuntime.run_state()` behavior
- new dependencies
- real provider enablement

---

## Completion Criteria

Phase 1 is complete when:

- `CompositeEventSink` and `CompositeTraceStore` exist.
- Dispatch diagnostics are prompt-safe.
- Failing secondary observers do not fail the assistant run by default.
- Trace reads remain primary-only.
- Focused tests pass.
- `docs/observability-harness.md` documents the composition layer.

Stop after this. The next phase is a separate design cycle for observer-only HookManager and metrics/audit/export adapters.
