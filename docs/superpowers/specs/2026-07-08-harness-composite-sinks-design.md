# Harness Composite Sinks Design

## Goal

Build the first low-risk harness hook foundation for `assistant_agent` by making the existing event and trace observation exits composable. This phase must preserve current runtime behavior while preparing a clean path toward a fuller industry-grade HookManager.

## Background

The current harness already has strong built-in lifecycle surfaces:

- `AgentEvent` / `EventSink` for live runtime, CLI, WebSocket, Gateway, realtime, and tests.
- `TraceEvent` / `TraceStore` for redacted timeline persistence and query.
- Boundary-specific observability wrappers for tool execution, context building, memory load/save, final response, and Gateway event mapping.

The immediate gap is not the absence of lifecycle signals. The gap is that event and trace outputs are mostly single-destination protocols. A production-grade harness needs several independent consumers, such as debug collectors, metrics, audit stores, local JSONL traces, optional exporters, and test probes, without making the assistant runtime know about each consumer.

## Scope

Phase 1 adds two small composition primitives:

- `CompositeEventSink`
- `CompositeTraceStore`

These primitives provide ordered fan-out, failure isolation, and prompt-safe diagnostics. They do not add intercepting hooks, policy hooks, plugin discovery, OpenTelemetry export, async dispatch, or user-facing debug UI.

## Non-Goals

This phase must not:

- Change assistant decisions, tool selection, memory policy, or provider behavior.
- Add a generic `HookManager`.
- Add `before_run`, `after_run`, `before_tool`, `after_tool`, or `before_llm` interception APIs.
- Allow hook handlers to bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Merge trace reads across multiple stores.
- Add new dependencies.
- Enable real external providers.

## Components

### `CompositeEventSink`

`CompositeEventSink` implements the existing `EventSink` protocol.

Constructor:

```python
class CompositeEventSink:
    def __init__(
        self,
        sinks: Iterable[EventSink],
        *,
        continue_on_error: bool = True,
    ) -> None: ...
```

Behavior:

- Calls child sinks in registration order.
- Sends the same `AgentEvent` instance to each child sink.
- If a child sink raises and `continue_on_error=True`, records a prompt-safe diagnostic and continues.
- If a child sink raises and `continue_on_error=False`, records the diagnostic and re-raises.
- Exposes diagnostics through `errors`.
- Does not emit additional `AgentEvent` records automatically, to avoid recursive sink loops.

### `CompositeTraceStore`

`CompositeTraceStore` implements the existing `TraceStore` protocol.

Constructor:

```python
class CompositeTraceStore:
    def __init__(
        self,
        primary: TraceStore,
        secondaries: Iterable[TraceStore] = (),
        *,
        continue_on_error: bool = True,
    ) -> None: ...
```

Behavior:

- `append()` writes to the primary store first, then each secondary store in registration order.
- If a store raises and `continue_on_error=True`, records a prompt-safe diagnostic and continues.
- If a store raises and `continue_on_error=False`, records the diagnostic and re-raises.
- Read operations delegate only to the primary store:
  - `list_by_run`
  - `list_by_trace`
  - `node_path`
  - `list_by_user`
- `delete_by_user()` deletes from primary and secondaries, returning the primary delete count. Secondary failures are handled with the same diagnostic policy.
- Reads do not merge stores. This keeps `/runs/{run_id}`, `/traces/{trace_id}`, and trace tooling deterministic and avoids duplicate events.

### Diagnostics

Add a small prompt-safe diagnostic model named `HookDispatchError`.

Required fields:

```text
target_index
target_name
operation
event_type
canonical_event
message
```

Rules:

- `message` is sanitized with the existing trace/provider error sanitizer.
- Diagnostics must not include raw event payloads, raw trace summaries, memory text, prompts, provider payloads, API keys, or command outputs.
- `target_name` is derived from the target class name.
- `event_type` is `AgentEvent.type` for event sinks or `TraceEvent.event_type` for trace stores when available.
- `canonical_event` is `TraceEvent.canonical_event` when available.

## Data Flow

Runtime event flow after this phase:

```text
AgentGraphRuntime / ToolExecutor / assistant loop
  -> EventSink.emit(AgentEvent)
  -> CompositeEventSink
  -> child sinks in order
```

Trace event flow after this phase:

```text
append_observability_event / trace helpers / ToolExecutor
  -> TraceStore.append(TraceEvent)
  -> CompositeTraceStore
  -> primary TraceStore
  -> secondary TraceStore(s)
```

Read flow:

```text
TraceQueryService / scripts / API debug routes
  -> CompositeTraceStore.list_by_run/list_by_trace/...
  -> primary TraceStore only
```

## Error Handling

The default policy is best-effort observability:

- A failing secondary sink/store must not fail an assistant run.
- A failing primary trace append is recorded and, by default, the composite still attempts secondaries. Reads remain primary-only, so a failed primary write can make the primary trace incomplete.
- `continue_on_error=False` is available for tests and strict operators who want fail-fast behavior.

The composite classes do not log directly. They expose `errors` so tests, scripts, or future operator surfaces can inspect dispatch failures explicitly.

## Testing Requirements

Add focused tests for:

- `CompositeEventSink` fan-out order.
- `CompositeEventSink` records a sanitized error and continues after a failing child.
- `CompositeEventSink` re-raises when `continue_on_error=False`.
- `CompositeTraceStore.append()` writes primary then secondaries.
- `CompositeTraceStore` reads only from primary.
- `CompositeTraceStore.delete_by_user()` returns the primary delete count.
- `CompositeTraceStore` records sanitized secondary failures without raw payloads.
- Existing runtime can use `CompositeEventSink` without losing emitted event order.

Targeted verification:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_harness_composite_sinks.py tests/test_agent_events.py -q
```

Diff hygiene:

```bash
git diff --check -- docs/superpowers/specs src/assistant_agent/services tests
```

## Future Path To Full HookManager

This phase intentionally stops at composition. The next phases can build on this without breaking users:

1. Add a named hook-event vocabulary that maps current `AgentEvent` and `TraceEvent` records to stable hook phases.
2. Add observer-only `HookManager` registration for `on_run_event`, `on_trace_event`, and `on_hook_error`.
3. Add optional metrics, audit, and exporter adapters as hook observers.
4. Consider intercepting hooks only after the observer-only path is stable, and only at safe governance boundaries. Interception must never bypass tool validation, execution policy, memory policy, provider policy, cancellation, or Gateway output gates.

## Stop Criteria For Phase 1

Stop this phase after the composite sinks/stores and tests are complete. Do not continue into generic HookManager work in the same phase. Moving to the full industry-grade harness should be a separate design and implementation cycle driven by concrete debugging, metrics, audit, or export needs.
