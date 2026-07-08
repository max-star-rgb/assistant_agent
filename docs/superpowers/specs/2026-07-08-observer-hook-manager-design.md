# Observer Hook Manager Design

## Goal

Add the second harness phase: an observer-only `HookManager` vocabulary layer that can receive runtime events, trace events, and hook dispatch failures without changing assistant behavior.

This phase moves the project closer to an industry-grade harness while preserving the current governance boundary:

```text
AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry
```

## Background

Phase 1 added:

- `CompositeEventSink` for ordered fan-out of `AgentEvent` streams.
- `CompositeTraceStore` for ordered fan-out of trace writes with primary-only reads.
- `HookDispatchError` diagnostics for prompt-safe observer failure reporting.

Those primitives make event and trace outputs composable. Phase 2 adds a named hook vocabulary and manager on top of those outputs, but still only as observers.

## Scope

Phase 2 adds:

- `HookObserver` protocol.
- `HookManager` observer dispatcher.
- `HookEventSink` adapter from `AgentEvent` to `HookManager`.
- `HookTraceStore` adapter from `TraceEvent` writes to `HookManager`.
- Focused tests and observability documentation.

## Non-Goals

This phase must not:

- Add intercepting hooks.
- Add `before_tool`, `after_tool`, `before_llm`, `after_llm`, or policy mutation hooks.
- Change `AgentGraphRuntime`, `ToolExecutor`, memory, provider, or Gateway behavior.
- Add plugin discovery, config loading, metrics exporters, audit exporters, OpenTelemetry, or UI.
- Allow observers to block or alter tool execution.
- Add dependencies.

## API

### `HookObserver`

```python
class HookObserver(Protocol):
    def on_run_event(self, event: AgentEvent) -> None: ...
    def on_trace_event(self, event: TraceEvent) -> None: ...
    def on_hook_error(self, error: HookDispatchError) -> None: ...
```

Observer methods are optional in practice. The manager should call a method only when the observer exposes it.

### `HookManager`

```python
class HookManager:
    def __init__(
        self,
        observers: Iterable[object] = (),
        *,
        continue_on_error: bool = True,
    ) -> None: ...

    def add_observer(self, observer: object) -> None: ...
    def on_run_event(self, event: AgentEvent) -> None: ...
    def on_trace_event(self, event: TraceEvent) -> None: ...
    def on_hook_error(self, error: HookDispatchError) -> None: ...

    @property
    def errors(self) -> list[HookDispatchError]: ...
```

Behavior:

- Observers are called in registration order.
- Missing observer methods are skipped.
- If an observer raises and `continue_on_error=True`, the manager records a `HookDispatchError`, then dispatches that error to observers that implement `on_hook_error`.
- If an observer raises and `continue_on_error=False`, the manager records and dispatches the error, then re-raises.
- `on_hook_error` failures are recorded but must not recursively dispatch additional hook errors.
- `errors` returns a copy.

### `HookEventSink`

```python
class HookEventSink:
    def __init__(self, manager: HookManager) -> None: ...
    def emit(self, event: AgentEvent) -> None: ...
```

This adapter implements the existing `EventSink` shape. It lets callers compose `HookManager` into `CompositeEventSink` without changing runtime code.

### `HookTraceStore`

```python
class HookTraceStore:
    def __init__(self, manager: HookManager) -> None: ...
    def append(self, event: TraceEvent) -> None: ...
    def list_by_run(self, run_id: str) -> list[TraceEvent]: ...
    def list_by_trace(self, trace_id: str) -> list[TraceEvent]: ...
    def node_path(self, run_id: str) -> list[str]: ...
    def list_by_user(self, user_id: str) -> list[TraceEvent]: ...
    def delete_by_user(self, user_id: str) -> int: ...
```

`HookTraceStore` is a write-only observer adapter. It forwards `append()` to the manager and returns empty values for read/delete methods. It is intended for use as a secondary store in `CompositeTraceStore`, not as a primary trace store.

## Data Flow

Runtime event path:

```text
AgentGraphRuntime / ToolExecutor
  -> CompositeEventSink
  -> existing sinks
  -> HookEventSink
  -> HookManager.on_run_event
  -> observers
```

Trace event path:

```text
append_observability_event / trace helpers / ToolExecutor
  -> CompositeTraceStore(primary=real store, secondaries=[HookTraceStore])
  -> HookManager.on_trace_event
  -> observers
```

Hook error path:

```text
observer failure
  -> HookDispatchError
  -> HookManager.errors
  -> HookManager.on_hook_error
  -> observers that expose on_hook_error
```

## Error Handling

The default is best-effort observer dispatch. A hook observer failure must not fail an assistant run or trace write.

Diagnostics reuse the Phase 1 `HookDispatchError` model. Error messages are sanitized with the existing provider error sanitizer and must not include raw prompts, memory content, provider payloads, event payload dumps, secrets, base64, or private paths.

## Testing Requirements

Add focused tests for:

- Observer registration order for `on_run_event`.
- Observer registration order for `on_trace_event`.
- Missing observer methods are ignored.
- Observer failure records `HookDispatchError` and notifies `on_hook_error` observers.
- `on_hook_error` failures do not recursively dispatch.
- Fail-fast mode re-raises observer errors after recording diagnostics.
- `HookEventSink` forwards `AgentEvent` to `HookManager`.
- `HookTraceStore` forwards `TraceEvent` to `HookManager` and remains read-empty.
- Runtime can use `CompositeEventSink([ListEventSink(), HookEventSink(manager)])`.
- `CompositeTraceStore(primary, [HookTraceStore(manager)])` preserves primary reads.

Targeted verification:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_hook_manager.py tests/test_harness_composite_sinks.py tests/test_agent_events.py -q
```

Diff hygiene:

```bash
git diff --check -- docs/observability-harness.md docs/superpowers/specs src/assistant_agent/services tests
```

## Future Path

Phase 3 should add concrete observer adapters only after this manager is stable:

1. Local metrics observer.
2. Local audit observer.
3. Optional exporter observer.
4. Operator/debug surfaces for hook diagnostics.

Interception hooks remain out of scope until there is a concrete need and a separate design.

## Stop Criteria For Phase 2

Stop this phase after observer-only `HookManager`, adapters, tests, and docs are complete. Do not implement metrics, audit, export, plugin discovery, or interception in Phase 2.
