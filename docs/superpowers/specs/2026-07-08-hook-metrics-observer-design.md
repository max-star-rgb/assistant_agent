# Hook Metrics Observer Design

## Goal

Add a local, observer-only metrics adapter for the harness hook system. The
adapter should let developers attach a hook observer and inspect aggregate
trace metrics without changing assistant runtime behavior or introducing an
external metrics stack.

## Context

The project already has three relevant pieces:

- `CompositeTraceStore` fans out trace writes while keeping reads primary-only.
- `HookTraceStore` forwards trace writes to `HookManager` observers.
- `build_trace_metrics()` aggregates run, tool, LLM, context, Gateway, and
  memory metrics from redacted `TraceEvent` records.

Phase 3 should connect these pieces rather than inventing a new metrics
pipeline.

## Recommended Approach

Create `TraceMetricsObserver`, a small in-memory observer that implements
`on_trace_event(event: TraceEvent) -> None`.

The observer will:

- store a redacted copy of each trace event it receives;
- expose `events` as a defensive copy for tests and local inspection;
- expose `summary() -> dict[str, Any]` by delegating to `build_trace_metrics()`;
- expose `clear() -> None` for local reset between runs.

This keeps `HookManager` responsible only for dispatch and keeps metrics
aggregation in the existing `trace_metrics` module.

## Alternatives Considered

### Metrics logic inside `HookManager`

This would make the API convenient, but it gives the manager two jobs:
dispatching observers and aggregating domain metrics. That makes future audit,
export, and notification observers harder to reason about.

### Metrics exporter now

An OpenTelemetry or Prometheus-style exporter is closer to a full production
observability stack, but it is premature. The local metrics contract should
stabilize before adding exporter semantics, configuration, labels, and
transport failure behavior.

### New metrics store

A separate persistent metrics store would duplicate trace storage and create a
new retention/privacy boundary. The current phase only needs local derived
metrics.

## Data Flow

```text
runtime
  -> append_observability_event(...)
  -> CompositeTraceStore(primary, [HookTraceStore(manager)])
  -> HookManager.on_trace_event(event)
  -> TraceMetricsObserver.on_trace_event(event)
  -> TraceMetricsObserver.summary()
  -> build_trace_metrics(redacted_events)
```

`CompositeTraceStore` remains the read owner. `HookTraceStore` returns empty
query results and is only used as a secondary fan-out adapter.

## API

```python
class TraceMetricsObserver:
    def __init__(self, events: Iterable[TraceEvent] = ()) -> None: ...

    @property
    def events(self) -> list[TraceEvent]: ...

    def on_trace_event(self, event: TraceEvent) -> None: ...

    def summary(self) -> dict[str, Any]: ...

    def clear(self) -> None: ...
```

## Safety

The observer stores redacted events by calling the same trace redaction boundary
used by `InMemoryTraceStore` and `JsonlTraceStore`. It must not retain raw
provider payloads, prompt bodies, memory content, API keys, tokens, or full
command/media payloads.

The observer is in-process and local. It does not send metrics to external
services, open network connections, or enable real provider paths.

## Error Handling

`TraceMetricsObserver` should not raise for normal trace events. If future code
adds stricter validation and an observer error occurs, `HookManager` already
records a redacted `HookDispatchError` and follows its configured
`continue_on_error` behavior.

## Tests

Add focused tests that verify:

- the observer collects trace events through `on_trace_event`;
- stored events are redacted and returned as a defensive copy;
- `summary()` delegates to the existing metrics shape;
- `clear()` resets local observer state;
- `HookManager` + `HookTraceStore` + `CompositeTraceStore` can feed the
  observer without changing primary trace reads.

## Non-Goals

This phase does not add:

- metrics exporters;
- Prometheus/OpenTelemetry labels or transport;
- debug API endpoints;
- UI/dashboard behavior;
- async/background flush behavior;
- plugin discovery;
- hook interception, cancellation, mutation, or policy decisions.

## Stop Criteria

Stop this phase after the local metrics observer, tests, and observability
documentation update are complete. The next phase should be justified by a
specific debugging gap, not by adding more hook surface area preemptively.
