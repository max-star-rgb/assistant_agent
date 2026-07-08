# Hook Invariant Observer Design

## Goal

Add a local, observer-only audit layer that validates core harness lifecycle
invariants from trace events and hook dispatch errors. The observer should help
tests and local debugging catch broken event sequencing without changing
assistant runtime behavior.

## Context

The hook system now has:

- `CompositeEventSink` and `CompositeTraceStore` for fan-out composition.
- `HookManager`, `HookEventSink`, and `HookTraceStore` for observer-only
  lifecycle dispatch.
- `TraceMetricsObserver` for in-process metrics derived from redacted trace
  events.

The next useful industry-aligned layer is invariant auditing: a local observer
that can tell whether a run trace is structurally coherent.

## Recommended Approach

Create `TraceInvariantObserver`, a sibling of `TraceMetricsObserver`.

The observer will:

- store redacted trace events received through `on_trace_event`;
- store hook dispatch errors received through `on_hook_error`;
- expose `violations() -> list[TraceInvariantViolation]`;
- expose `is_valid() -> bool`, `events`, `hook_errors`, and `clear()`;
- avoid raising during event dispatch.

`TraceInvariantViolation` will be a prompt-safe dataclass with a stable
`code`, human-readable `message`, optional run/trace/tool/event identifiers, and
sanitized low-cardinality `detail`.

## Initial Invariants

This phase should implement only these local invariants:

1. Every `run.started` has exactly one terminal `run.completed`, `run.failed`,
   or `run.cancelled` event for the same `run_id`.
2. Every `tool.started` has a matching `tool.finished` or `tool.failed` for the
   same tool call key.
3. Every `tool.observation` references a prior tool lifecycle event or a prior
   validation rejection in the same run.
4. Hook dispatch error messages are already redacted before the observer sees
   them.

The tool call key should prefer `tool_call_id` or `call_id` from trace
attributes, input summary, output summary, or error detail. When no call ID is
available, it can fall back to `(run_id, tool_name)` as a compatibility path
for older traces.

## Alternatives Considered

### Put invariants inside `HookManager`

This would make invariant checking automatic, but it would turn `HookManager`
from a dispatch primitive into a stateful audit engine. That makes future
observers harder to isolate and increases the chance that auditing changes
runtime behavior.

### Add pytest-only helpers

Pytest helpers would catch regressions, but they would not be reusable for local
debugging, future CLI commands, or in-process harness composition.

### Add an exporter or dashboard now

Exporters and dashboards need stable event semantics first. This phase should
only validate local event coherence.

## Data Flow

```text
runtime trace writes
  -> CompositeTraceStore(primary, [HookTraceStore(manager)])
  -> HookManager.on_trace_event(event)
  -> TraceInvariantObserver.on_trace_event(event)
  -> TraceInvariantObserver.violations()
```

Hook errors follow the same observer path:

```text
observer failure
  -> HookManager records HookDispatchError
  -> HookManager.on_hook_error(error)
  -> TraceInvariantObserver.on_hook_error(error)
```

The observer does not own persistence. `CompositeTraceStore` remains the read
owner for trace queries.

## API

```python
@dataclass(frozen=True)
class TraceInvariantViolation:
    code: str
    message: str
    run_id: str | None = None
    trace_id: str | None = None
    canonical_event: str | None = None
    tool_name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class TraceInvariantObserver:
    def __init__(
        self,
        events: Iterable[TraceEvent] = (),
        hook_errors: Iterable[HookDispatchError] = (),
    ) -> None: ...

    @property
    def events(self) -> list[TraceEvent]: ...

    @property
    def hook_errors(self) -> list[HookDispatchError]: ...

    def on_trace_event(self, event: TraceEvent) -> None: ...

    def on_hook_error(self, error: HookDispatchError) -> None: ...

    def violations(self) -> list[TraceInvariantViolation]: ...

    def is_valid(self) -> bool: ...

    def clear(self) -> None: ...
```

## Safety

The observer stores trace events through the same redaction boundary used by
trace stores. Violation records must not include raw prompts, memory content,
provider payloads, API keys, tokens, Authorization headers, media payloads, or
full command outputs.

Hook error redaction checks should never copy the raw hook error message into a
violation. They should report only stable metadata such as target name,
operation, event type, and canonical event.

## Error Handling

The observer should be passive. It must not throw for incomplete traces, missing
terminal events, malformed sequencing, or unredacted hook error messages.
Callers inspect `violations()` or `is_valid()` after the relevant run/test has
finished.

## Tests

Add focused tests that verify:

- a valid run/tool lifecycle has no violations;
- a run with `run.started` but no terminal run event is reported;
- a tool with `tool.started` but no terminal tool event is reported;
- a `tool.observation` without a prior tool lifecycle event or validation
  rejection is reported;
- a failed tool without required error detail is reported for missing code or
  recovery action;
- stored trace events are redacted;
- unredacted hook dispatch errors are reported without leaking the raw secret;
- the observer composes through `HookManager`, `HookTraceStore`, and
  `CompositeTraceStore` without changing primary trace reads.

## Non-Goals

This phase does not add:

- exporters;
- dashboards;
- API endpoints;
- pytest plugins;
- async/background audit workers;
- runtime cancellation or policy decisions;
- mutation/interception hooks;
- new canonical event types.

## Stop Criteria

Stop this phase after the invariant observer, tests, and observability
documentation update are complete. The next phase should be driven by a
specific debugging gap, not by adding hook surfaces preemptively.
