# Hook Metrics Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local in-memory hook observer that derives metrics from redacted trace events.

**Architecture:** `TraceMetricsObserver` lives in a focused service module and implements only the trace-observer side of the hook protocol. It redacts incoming events, stores an in-memory event list, and delegates aggregation to `build_trace_metrics()`.

**Tech Stack:** Python, Pydantic `TraceEvent`, existing trace redaction, existing pytest suite.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep the default path mock/local/offline; do not call real providers.
- Use `apply_patch` for manual edits.
- Do not add dependencies.
- Do not add metrics exporters, API endpoints, dashboards, plugin discovery, or hook interception.
- Hook observers must not mutate assistant runtime behavior.
- Stored observer events must pass through the same redaction boundary as trace stores.

---

### Task 1: Add Metrics Observer Behavior Tests

**Files:**
- Create: `tests/test_hook_metrics.py`

**Interfaces:**
- Consumes: `TraceEvent` from `assistant_agent.services.trace_store`, `HookManager` and `HookTraceStore` from `assistant_agent.services.hooks`, `CompositeTraceStore` and `InMemoryTraceStore` from `assistant_agent.services.trace_store`.
- Produces: test expectations for `TraceMetricsObserver(events=())`, `events`, `on_trace_event(event)`, `summary()`, and `clear()`.

- [ ] **Step 1: Write the failing tests**

```python
import json

from assistant_agent.services.hook_metrics import TraceMetricsObserver
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent


def _event(
    *,
    canonical_event: str = "run.started",
    status: str | None = "started",
    run_id: str = "run_1",
    trace_id: str = "trace_1",
    node_name: str = "runtime",
    tool_name: str | None = None,
    latency_ms: int | None = None,
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
        latency_ms=latency_ms,
        attributes=attributes or {},
        error=error,
    )


def test_trace_metrics_observer_collects_redacted_trace_events() -> None:
    observer = TraceMetricsObserver()
    raw_event = _event(attributes={"api_key": "sk-secret-value", "safe_count": 1})

    observer.on_trace_event(raw_event)

    assert len(observer.events) == 1
    dumped = json.dumps([event.model_dump(mode="json") for event in observer.events])
    assert "sk-secret-value" not in dumped
    assert observer.events[0].attributes["safe_count"] == 1


def test_trace_metrics_observer_events_are_defensive_copy() -> None:
    observer = TraceMetricsObserver([_event()])

    observer.events.clear()

    assert len(observer.events) == 1


def test_trace_metrics_observer_summary_uses_existing_metrics_shape() -> None:
    observer = TraceMetricsObserver()
    observer.on_trace_event(_event(canonical_event="run.started", status="started"))
    observer.on_trace_event(
        _event(
            canonical_event="tool.failed",
            status="failed",
            node_name="tool_executor",
            tool_name="product_search",
            latency_ms=80,
            attributes={"retry_count": 1},
            error={"code": "provider_timeout", "message": "Provider timed out"},
        )
    )
    observer.on_trace_event(_event(canonical_event="run.failed", status="failed"))

    metrics = observer.summary()

    assert metrics["event_count"] == 3
    assert metrics["run"]["count"] == 1
    assert metrics["run"]["failed"] == 1
    assert metrics["tools"]["total_calls"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["failure_count"] == 1


def test_trace_metrics_observer_clear_resets_local_state() -> None:
    observer = TraceMetricsObserver([_event()])

    observer.clear()

    assert observer.events == []
    assert observer.summary()["event_count"] == 0


def test_trace_metrics_observer_composes_through_hook_trace_store_without_changing_primary_reads() -> None:
    primary = InMemoryTraceStore()
    observer = TraceMetricsObserver()
    manager = HookManager([observer])
    store = CompositeTraceStore(primary, [HookTraceStore(manager)])
    event = _event()

    store.append(event)

    assert store.list_by_run("run_1") == primary.list_by_run("run_1")
    assert observer.summary()["event_count"] == 1
    assert manager.errors == []
```

- [ ] **Step 2: Run tests to verify they fail because the module does not exist**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_metrics.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.hook_metrics'`.

### Task 2: Implement `TraceMetricsObserver`

**Files:**
- Create: `src/assistant_agent/services/hook_metrics.py`
- Test: `tests/test_hook_metrics.py`

**Interfaces:**
- Consumes: `build_trace_metrics(events: list[TraceEvent]) -> dict[str, Any]`, `TraceEvent`, `redact_trace_event(event: TraceEvent) -> TraceEvent`.
- Produces: `TraceMetricsObserver` with `__init__`, `events`, `on_trace_event`, `summary`, and `clear`.

- [ ] **Step 1: Add the implementation**

```python
"""Metrics observers for observer-only harness hooks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from assistant_agent.services.trace_metrics import build_trace_metrics
from assistant_agent.services.trace_store import TraceEvent, redact_trace_event


class TraceMetricsObserver:
    """In-memory metrics observer derived from redacted trace events."""

    def __init__(self, events: Iterable[TraceEvent] = ()) -> None:
        self._events = [redact_trace_event(event) for event in events]

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def on_trace_event(self, event: TraceEvent) -> None:
        self._events.append(redact_trace_event(event))

    def summary(self) -> dict[str, Any]:
        return build_trace_metrics(self._events)

    def clear(self) -> None:
        self._events.clear()
```

- [ ] **Step 2: Run the focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_metrics.py -q
```

Expected: PASS.

### Task 3: Document Observer Metrics Composition

**Files:**
- Modify: `docs/observability-harness.md`
- Test: `tests/test_hook_metrics.py`, `tests/test_hook_manager.py`, `tests/test_trace_metrics.py`

**Interfaces:**
- Consumes: `TraceMetricsObserver`, `HookTraceStore`, `HookManager`, `build_trace_metrics()`.
- Produces: updated observability harness docs that identify the metrics observer as local, in-process, redacted, and non-exporting.

- [ ] **Step 1: Update the observability docs**

Add this content near the current hook composition paragraph and metrics section:

```markdown
`TraceMetricsObserver` is the local in-memory metrics observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and exposes the same aggregate shape as
`build_trace_metrics()`. It is a developer harness helper, not a metrics
exporter, dashboard, policy hook, or API surface.
```

- [ ] **Step 2: Run focused regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_metrics.py tests/test_hook_manager.py tests/test_trace_metrics.py -q
```

Expected: PASS.

- [ ] **Step 3: Run the fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: PASS.

- [ ] **Step 4: Commit implementation**

```bash
git add src/assistant_agent/services/hook_metrics.py tests/test_hook_metrics.py docs/observability-harness.md
git commit -m "feat: add hook metrics observer"
```
