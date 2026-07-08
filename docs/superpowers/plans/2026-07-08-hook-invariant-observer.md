# Hook Invariant Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local in-memory hook observer that reports prompt-safe trace lifecycle invariant violations.

**Architecture:** `TraceInvariantObserver` lives in a focused service module beside `TraceMetricsObserver`. It stores redacted trace events and hook dispatch errors, then computes invariant violations on demand without changing `HookManager` dispatch behavior.

**Tech Stack:** Python, dataclasses, Pydantic `TraceEvent`, existing trace redaction, existing pytest suite.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Use `apply_patch` for manual edits.
- Do not add dependencies.
- Do not add exporters, dashboards, API endpoints, pytest plugins, async workers, runtime cancellation, policy decisions, mutation hooks, interception hooks, or new canonical event types.
- Hook observers must not mutate assistant runtime behavior.
- Stored trace events must pass through the same redaction boundary as trace stores.
- Hook error redaction checks must not copy raw hook error messages into violation records.

---

### Task 1: Add Invariant Observer Behavior Tests

**Files:**
- Create: `tests/test_hook_invariants.py`

**Interfaces:**
- Consumes: `TraceEvent`, `CompositeTraceStore`, `InMemoryTraceStore`, `HookManager`, `HookTraceStore`, and `HookDispatchError`.
- Produces: expected API for `TraceInvariantObserver`, `TraceInvariantViolation`, `events`, `hook_errors`, `on_trace_event`, `on_hook_error`, `violations`, `is_valid`, and `clear`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail because the module does not exist**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_invariants.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.hook_invariants'`.

### Task 2: Implement `TraceInvariantObserver`

**Files:**
- Create: `src/assistant_agent/services/hook_invariants.py`
- Test: `tests/test_hook_invariants.py`

**Interfaces:**
- Consumes: `TraceEvent`, `redact_trace_event(event: TraceEvent) -> TraceEvent`, `HookDispatchError`, `sanitize_error_detail(value)`, `sanitize_error_message(value)`.
- Produces: `TraceInvariantViolation` dataclass and `TraceInvariantObserver` with the API from Task 1.

- [ ] **Step 1: Add the implementation**

```python
"""Invariant observers for observer-only harness hooks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.services.hook_dispatch import HookDispatchError
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.trace_store import TraceEvent, redact_trace_event


TERMINAL_RUN_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
TERMINAL_TOOL_EVENTS = {"tool.finished", "tool.failed"}
TOOL_LIFECYCLE_EVENTS = {"tool.started", *TERMINAL_TOOL_EVENTS}
VALIDATION_REJECTION_STATUSES = {"rejected", "blocked", "failed"}


@dataclass(frozen=True)
class TraceInvariantViolation:
    """Prompt-safe trace lifecycle invariant violation."""

    code: str
    message: str
    run_id: str | None = None
    trace_id: str | None = None
    canonical_event: str | None = None
    tool_name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class TraceInvariantObserver:
    """In-memory observer that audits redacted trace event sequencing."""

    def __init__(
        self,
        events: Iterable[TraceEvent] = (),
        hook_errors: Iterable[HookDispatchError] = (),
    ) -> None:
        self._events = [redact_trace_event(event) for event in events]
        self._hook_errors = list(hook_errors)

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    @property
    def hook_errors(self) -> list[HookDispatchError]:
        return list(self._hook_errors)

    def on_trace_event(self, event: TraceEvent) -> None:
        self._events.append(redact_trace_event(event))

    def on_hook_error(self, error: HookDispatchError) -> None:
        self._hook_errors.append(error)

    def violations(self) -> list[TraceInvariantViolation]:
        return [
            *self._run_lifecycle_violations(),
            *self._tool_lifecycle_violations(),
            *self._tool_observation_violations(),
            *self._failed_tool_detail_violations(),
            *self._hook_error_violations(),
        ]

    def is_valid(self) -> bool:
        return not self.violations()

    def clear(self) -> None:
        self._events.clear()
        self._hook_errors.clear()

    def _run_lifecycle_violations(self) -> list[TraceInvariantViolation]:
        by_run: dict[str, list[TraceEvent]] = defaultdict(list)
        for event in self._events:
            if event.canonical_event == "run.started" or event.canonical_event in TERMINAL_RUN_EVENTS:
                by_run[event.run_id].append(event)

        violations: list[TraceInvariantViolation] = []
        for run_id, events in sorted(by_run.items()):
            starts = [event for event in events if event.canonical_event == "run.started"]
            terminals = [event for event in events if event.canonical_event in TERMINAL_RUN_EVENTS]
            anchor = starts[0] if starts else terminals[0]
            if len(starts) > 1:
                violations.append(
                    _violation(
                        "multiple_run_started",
                        "Run has more than one run.started event.",
                        anchor,
                        detail={"started_count": len(starts)},
                    )
                )
            if starts and not terminals:
                violations.append(
                    _violation("missing_run_terminal", "Run started without a terminal run event.", anchor)
                )
            if starts and len(terminals) > 1:
                violations.append(
                    _violation(
                        "multiple_run_terminal",
                        "Run has more than one terminal run event.",
                        terminals[-1],
                        detail={"terminal_count": len(terminals)},
                    )
                )
            if terminals and not starts:
                violations.append(
                    _violation(
                        "terminal_without_run_started",
                        "Run terminal event was emitted without run.started.",
                        terminals[0],
                    )
                )
        return violations

    def _tool_lifecycle_violations(self) -> list[TraceInvariantViolation]:
        starts_by_key: dict[tuple[str, str, str], list[TraceEvent]] = defaultdict(list)
        terminals_by_key: dict[tuple[str, str, str], list[TraceEvent]] = defaultdict(list)
        for event in self._events:
            if event.canonical_event not in TOOL_LIFECYCLE_EVENTS:
                continue
            key = _tool_call_key(event)
            if key is None:
                continue
            if event.canonical_event == "tool.started":
                starts_by_key[key].append(event)
            elif event.canonical_event in TERMINAL_TOOL_EVENTS:
                terminals_by_key[key].append(event)

        violations: list[TraceInvariantViolation] = []
        for key, starts in sorted(starts_by_key.items()):
            terminals = terminals_by_key.get(key, [])
            if not terminals:
                violations.append(
                    _violation(
                        "missing_tool_terminal",
                        "Tool started without a terminal tool event.",
                        starts[0],
                        detail={"tool_call_key": _format_tool_call_key(key)},
                    )
                )
            if len(terminals) > len(starts):
                violations.append(
                    _violation(
                        "multiple_tool_terminal",
                        "Tool call has more terminal events than starts.",
                        terminals[-1],
                        detail={
                            "tool_call_key": _format_tool_call_key(key),
                            "started_count": len(starts),
                            "terminal_count": len(terminals),
                        },
                    )
                )
        for key, terminals in sorted(terminals_by_key.items()):
            if key not in starts_by_key:
                violations.append(
                    _violation(
                        "tool_terminal_without_start",
                        "Tool terminal event was emitted without tool.started.",
                        terminals[0],
                        detail={"tool_call_key": _format_tool_call_key(key)},
                    )
                )
        return violations

    def _tool_observation_violations(self) -> list[TraceInvariantViolation]:
        seen_tool_keys: set[tuple[str, str, str]] = set()
        rejected_runs: set[str] = set()
        rejected_keys: set[tuple[str, str, str]] = set()
        violations: list[TraceInvariantViolation] = []

        for event in self._events:
            key = _tool_call_key(event)
            if event.canonical_event in TOOL_LIFECYCLE_EVENTS and key is not None:
                seen_tool_keys.add(key)
                continue
            if event.canonical_event == "action.validation.finished" and _is_validation_rejection(event):
                rejected_runs.add(event.run_id)
                if key is not None:
                    rejected_keys.add(key)
                continue
            if event.canonical_event != "tool.observation":
                continue
            if key is not None and (key in seen_tool_keys or key in rejected_keys):
                continue
            if event.run_id in rejected_runs:
                continue
            violations.append(
                _violation(
                    "tool_observation_without_prior_action",
                    "Tool observation was emitted without a prior tool lifecycle event or validation rejection.",
                    event,
                    detail={"tool_call_key": _format_tool_call_key(key) if key is not None else None},
                )
            )
        return violations

    def _failed_tool_detail_violations(self) -> list[TraceInvariantViolation]:
        violations: list[TraceInvariantViolation] = []
        for event in self._events:
            if event.canonical_event != "tool.failed":
                continue
            error = event.error or {}
            error_code = event.error_code or _dict_str(error, "code")
            error_message = _dict_str(error, "message")
            recovery_action = _dict_str(error, "recovery_action") or _dict_str(event.attributes, "recovery_action")
            if not error_code:
                violations.append(
                    _violation("missing_tool_error_code", "Failed tool event is missing an error code.", event)
                )
            if not error_message:
                violations.append(
                    _violation("missing_tool_error_message", "Failed tool event is missing a redacted error message.", event)
                )
            if not recovery_action:
                violations.append(
                    _violation(
                        "missing_tool_recovery_action",
                        "Failed tool event is missing a recovery action.",
                        event,
                    )
                )
        return violations

    def _hook_error_violations(self) -> list[TraceInvariantViolation]:
        violations: list[TraceInvariantViolation] = []
        for error in self._hook_errors:
            if sanitize_error_message(error.message) == error.message:
                continue
            violations.append(
                TraceInvariantViolation(
                    code="hook_error_not_redacted",
                    message="Hook dispatch error message was not redacted before observer delivery.",
                    canonical_event=error.canonical_event,
                    detail=sanitize_error_detail(
                        {
                            "target_index": error.target_index,
                            "target_name": error.target_name,
                            "operation": error.operation,
                            "event_type": error.event_type,
                        }
                    ),
                )
            )
        return violations


def _violation(
    code: str,
    message: str,
    event: TraceEvent,
    *,
    detail: dict[str, Any] | None = None,
) -> TraceInvariantViolation:
    return TraceInvariantViolation(
        code=code,
        message=message,
        run_id=event.run_id,
        trace_id=event.trace_id,
        canonical_event=event.canonical_event,
        tool_name=event.tool_name,
        detail=sanitize_error_detail(detail or {}),
    )


def _tool_call_key(event: TraceEvent) -> tuple[str, str, str] | None:
    call_id = _tool_call_id(event)
    if call_id:
        return ("call", event.run_id, call_id)
    if event.tool_name:
        return ("tool", event.run_id, event.tool_name)
    return None


def _tool_call_id(event: TraceEvent) -> str | None:
    for mapping in (event.attributes, event.input_summary, event.output_summary, event.error or {}):
        if not isinstance(mapping, dict):
            continue
        for key in ("tool_call_id", "call_id"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _format_tool_call_key(key: tuple[str, str, str]) -> str:
    kind, run_id, value = key
    return f"{kind}:{run_id}:{value}"


def _is_validation_rejection(event: TraceEvent) -> bool:
    if event.status in VALIDATION_REJECTION_STATUSES:
        return True
    for key in ("validation_status", "confirmation_state", "decision"):
        value = _dict_str(event.attributes, key)
        if value in VALIDATION_REJECTION_STATUSES:
            return True
    return False


def _dict_str(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None
```

- [ ] **Step 2: Run the focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_invariants.py -q
```

Expected: PASS.

### Task 3: Document Invariant Observer Composition

**Files:**
- Modify: `docs/observability-harness.md`
- Test: `tests/test_hook_invariants.py`, `tests/test_hook_manager.py`, `tests/test_hook_metrics.py`, `tests/test_trace_metrics.py`

**Interfaces:**
- Consumes: `TraceInvariantObserver`, `HookTraceStore`, `HookManager`.
- Produces: updated observability harness docs that identify the invariant observer as local, passive, redacted, and non-exporting.

- [ ] **Step 1: Update the observability docs**

Add this content near the hook composition paragraph and harness invariants section:

```markdown
`TraceInvariantObserver` is the local in-memory audit observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and reports prompt-safe `TraceInvariantViolation` records
for broken run/tool sequencing or unredacted hook dispatch errors. It is passive:
violations are inspected after a run or test, and the observer does not raise,
cancel, export, or mutate runtime behavior.
```

- [ ] **Step 2: Run focused regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_invariants.py tests/test_hook_manager.py tests/test_hook_metrics.py tests/test_trace_metrics.py -q
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
git add src/assistant_agent/services/hook_invariants.py tests/test_hook_invariants.py docs/observability-harness.md
git commit -m "feat: add hook invariant observer"
```
