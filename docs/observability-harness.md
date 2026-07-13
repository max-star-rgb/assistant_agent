# Observability Harness

This document is the current entry for assistant run status, logs, monitoring,
trace, and ReAct checkpoint observability. It defines the developer-facing
harness contract for understanding one run end to end without exposing raw
provider payloads, full prompts, memory content, secrets, media bodies, or hidden
reasoning.

## Agent-Service Delivery Audit

`/agent-service/v1` writes prompt-safe delivery transitions to
`.data/agent_service_delivery.jsonl`. Records distinguish `accepted`,
`processing`, `sent`, `acked`, `failed`, `disconnected_before_send`, and
`disconnected_before_ack`. A `sent` record only proves that WebSocket
`send_text()` returned; only `acked` proves the media application processed the
final response.

Records use digests for session and chat identifiers and may include run/trace
ids, close code, and a close-reason category. They never include response text,
raw media, phone numbers, credentials, or provider payloads. This JSONL file is
local runtime evidence, not a durable cross-host delivery database.

## Realtime Video Observation

Realtime video observation remains visible through governed
`video_understanding` tool records rather than a second Provider-only trace
path. Structured tool data and contract metadata use one of these prompt-safe
sources:

- `background_keyframe_observation`: a selected keyframe was analyzed by the
  per-connection observer through `ActionValidator` and `ToolExecutor`;
- `rolling_video_memory`: a user query used the latest healthy semantic
  snapshot and made no query-time visual Provider call;
- `recent_frame_fallback`: semantic memory was absent, not ready, or latest
  failed, so the ordinary recent-frame Provider path ran.

Safe diagnostic fields are source, opaque video/output reference, snapshot
sequence, observed timestamp, keyframe count, queue counts, status, reason code,
provider/model name, and latency. Raw H.264, JPEG bytes, grayscale fingerprints,
absolute keyframe paths, Provider raw payloads, phone numbers, and user-visible
response text must not appear in trace summaries or delivery audit records.

`videoResponse(code=0)` is an ingestion signal: H.264 validation, JPEG and
fingerprint decode, context registration, and local selection scheduling
completed. It is not evidence that background MLLM observation completed.
Connection cleanup stops scheduling, rejects late semantic updates, then removes
rolling snapshots and both retained and raw JPEG artifacts.

## Purpose

The observability harness should answer five developer questions quickly:

1. What happened in this run?
2. Where did it stop, fail, cancel, or wait?
3. Why did the assistant choose a tool or final answer?
4. Which tool/provider/context/memory boundary contributed to the result?
5. Is the trace safe to inspect, return through APIs, and use in tests?

The harness is local-first. Internal event and trace semantics should stay close
to OpenTelemetry-style traces, metrics, and logs, but this project should not
require an external APM stack for normal development.

## Current Surfaces

| surface | owner | purpose |
| --- | --- | --- |
| `AgentState` | runtime | In-memory fact record for one run: status, tool calls, results, errors, and response. |
| `AgentEvent` / `EventSink` | runtime and entry layers | Real-time event stream for WebSocket, Gateway, realtime, CLI, and tests. |
| `TraceStore` / `TraceQueryService` | services | Redacted run and trace summaries for `/runs/{run_id}`, `/traces/{trace_id}`, and tool-call debug views. |
| `react_steps` / `decision_trace` | API response metadata | Compact per-response ReAct timeline for developer UI and CLI output. |
| `RunHistoryStore` / `ToolHistoryStore` / `SessionStore` | services | Local JSONL/session indexes and lifecycle ledgers. |
| Gateway frames | gateway | Realtime wire lifecycle: `run.started`, `event.progress`, `stream.chunk`, `run.end`, `run.cancel`, call hangup, and config updates. |

These surfaces may expose the same fact at different fidelity. The canonical
source for debug reconstruction should be the redacted trace timeline. API
response fields and realtime events are projections of that timeline or live
runtime lifecycle events.

Product entry layers should reach assistant execution through
`AssistantRuntimeApp` before entering `run_assistant_request` and
`AgentGraphRuntime`. Hook observers should attach after this application
runtime boundary is stable; they should not be wired separately inside Web,
CLI, or Gateway transport adapters.

For local composition, `CompositeEventSink` can fan out one runtime event stream
to several sinks, and `CompositeTraceStore` can fan out trace writes to a
primary store plus secondary stores while keeping reads primary-only. These are
observer composition primitives, not a generic HookManager and not interception
points for changing assistant behavior.
`HookManager` builds on those composition primitives as an observer-only
vocabulary layer. `HookEventSink` forwards `AgentEvent` records to observers,
and `HookTraceStore` forwards trace writes to observers when used as a secondary
store. Hook observers cannot intercept or mutate assistant behavior; they only
receive prompt-safe lifecycle records and hook dispatch errors.
`TraceMetricsObserver` is the local in-memory metrics observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and exposes the same aggregate shape as
`build_trace_metrics()`. It is a developer harness helper, not a metrics
exporter, dashboard, policy hook, or API surface.
`TraceInvariantObserver` is the local in-memory audit observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and reports prompt-safe `TraceInvariantViolation` records
for broken run/tool sequencing or unredacted hook dispatch errors. It is
passive: violations are inspected after a run or test, and the observer does
not raise, cancel, export, or mutate runtime behavior.

## Canonical IDs

Every run-observability record should carry the strongest available identity
chain:

```text
user_id
session_id
run_id
trace_id
span_id
parent_span_id
tool_call_id
turn_id
event_id
```

Required fields by layer:

- Run-level events require `run_id`, `trace_id`, `user_id`, and `session_id`.
- Tool events require `tool_name` and `tool_call_id` when a call has been
  allocated.
- Gateway/realtime events should preserve `turn_id` when available and include
  backend `run_id` / `trace_id` in terminal frames or payloads.
- Cross-agent or delegated work must preserve parent `run_id` / `trace_id`
  through the agent communication boundary.

`trace_id` is the cross-surface debug lookup key. `run_id` identifies one
assistant execution. `span_id` and `parent_span_id` are the future-proof local
shape for hierarchical traces and optional OpenTelemetry export.

## Canonical Timeline

Use stable dotted event names for the internal trace timeline. Existing
`AgentEvent`, Gateway frame names, and response metadata can keep their current
public names, but they should map to this vocabulary.

| canonical event | meaning |
| --- | --- |
| `run.started` | A user request has entered the assistant runtime. |
| `gateway.turn.started` | Gateway accepted a normalized user turn. |
| `memory.load.started` / `memory.load.finished` | Memory context load lifecycle. |
| `context.build.started` / `context.build.finished` | Prompt/native context pack construction and budget report. |
| `llm.chat.started` / `llm.chat.finished` | One provider chat call, including direct-answer and native tool-call responses. |
| `react.decision` | Assistant selected final answer, follow-up, tool call, or plan transition. |
| `action.validation.started` / `action.validation.finished` | Local validation before tool execution. |
| `tool.started` | Tool execution lifecycle began through `ToolExecutor`. |
| `tool.finished` | Tool returned successfully, including duplicate suppression or pending confirmation result. |
| `tool.failed` | Tool failed, budget was blocked, or cancellation interrupted execution. |
| `tool.observation` | Tool result was converted into assistant-facing observation data. |
| `loop_guard.triggered` | ReAct guard stopped or redirected the loop. |
| `response.delta` | User-visible response text chunk was emitted. |
| `response.final` | Final response was set. |
| `memory.save.started` / `memory.save.finished` | Post-run memory save or promotion lifecycle. |
| `run.completed` | Run ended successfully. |
| `run.failed` | Run ended with an error. |
| `run.cancelled` | Run ended through cooperative cancellation. |

The existing mock/offline LangGraph node path remains useful, but node names are
not the canonical event vocabulary. Native provider runtime must produce the
same high-level timeline even when it does not enter LangGraph node wrappers.
`ToolExecutor` is the canonical owner for `tool.started`, `tool.finished`, and
`tool.failed`; runtime layers may still emit `tool.observation` after converting
the result into assistant-facing data.
`AssistantContextPack` construction is wrapped by the context observability
helper, which emits `context.build.started` and `context.build.finished` with
redacted budget, source-count, compaction, and tool-catalog summaries.
Memory lifecycle calls are wrapped at the runtime and graph memory boundaries:
`memory.load.started` / `memory.load.finished` summarize retrieval/injection
counts, token budget fields, retrieval version, and injected memory IDs;
`memory.save.started` / `memory.save.finished` summarize promotion/save
decisions, skipped reasons, and written IDs. These events must not include
memory summaries, rendered memory context, candidate content, or raw user text.
Final response tracing emits `response.final` with only prompt-safe response
shape data such as message presence, character count, output-ref count, response
data keys, status, and error count. It must not include the response text.

## Span Model

Trace records should move toward a span-plus-event model:

```text
run
  gateway.turn
  memory.load
  context.build
  llm.chat(iteration=1)
  react.decision(iteration=1)
  action.validation
  tool.execute(product_search)
    provider.call(product_search_provider)
  tool.observation
  llm.chat(iteration=2)
  response.final
  memory.save
```

Minimal span fields:

```text
span_id
parent_span_id
name
event_type
status
started_at
finished_at
duration_ms
attributes
error
```

`attributes` must be prompt-safe and redacted. It can include tool names,
provider names, model names, token counts, latency, retry count, budget summary,
risk gate summary, side-effect level, confirmation state, output references,
context budget summaries, and compact error codes.

## Developer UX

The harness should optimize for a developer debugging locally.

Current query surfaces:

```bash
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
```

Target local CLI:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --errors
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --json
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --trace-path .data/graph_trace.jsonl
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --json
```

Human-readable output should show:

```text
run run_xxx trace trace_xxx status=failed duration=1832ms
01  run.started                         0ms
02  context.build.finished             12ms budget=42%
03  llm.chat.finished                  820ms provider=mock model=...
04  react.decision                       tool_call product_search
05  action.validation.finished           accepted
06  tool.started                         product_search risk=external_read
07  tool.failed                          provider_timeout retry=1
08  tool.observation                     failed recovery=retry_or_report
09  run.failed                           PROVIDER_TIMEOUT
```

Developer output should group errors first when requested, then show the full
timeline. The default view should be short enough to paste into an issue or a
handoff note. Metrics output should answer aggregate health questions without
opening individual traces first.

## Metrics

The first metrics layer should remain small and derived from trace/events where
possible:

| area | metrics |
| --- | --- |
| Run | count, success rate, failure rate, cancel rate, duration p50/p95. |
| Tool | call count by tool, latency, failure rate, retry count, risk gate decisions, confirmation-required count. |
| LLM | chat call count, latency, token usage, native tool-call rate, direct-answer rate, provider error count. |
| Context | total chars/tokens, budget ratio, compaction triggered count, overflow retry count. |
| Gateway/realtime | active runs, interrupt count, cancel source, hangup cancellation, deadline expiry. |
| Memory | retrieval count, save candidate count, confirmed/rejected count, promotion counters. |

Do not add high-cardinality labels such as raw prompts, raw queries, full URLs,
full memory text, full provider errors, or media payloads.

The local metrics command currently exposes this first layer:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py
```

It reads the redacted JSONL trace store, supports optional `--user-id` and
`--session-id` filters, and prints either a paste-friendly text summary or a
machine-readable `--json` summary. API/debug endpoint exposure should be added
only after the local metrics shape is stable. Tool metrics prefer terminal
`tool.finished` / `tool.failed` lifecycle events and only count
`tool.observation` for older traces that lack terminal tool events.
For in-process hook composition, `TraceMetricsObserver` exposes the same metrics
shape from trace events received during the current process. It is useful for
tests and local harness wiring where reading the JSONL trace store is
unnecessary.

## Trajectory Debug

`assistant_agent.services.trajectory_debug` provides the Phase 5 local
trajectory debug contract. It converts already-redacted `TraceEvent` records
into `TrajectoryReplayCase` objects for debug, replay preview, and regression
eval review.

Trajectory replay cases are diagnostic artifacts only:

- `replay_mode` is `debug_replay_eval_only`.
- `production_mutation_allowed` and `raw_data_included` are always false.
- Timeline entries keep only prompt-safe IDs, event names, statuses, tool names,
  provider/model labels, error codes, latency, span links, and allowlisted
  budget/retry/output-ref summaries.
- They do not include raw user text, prompts, rendered context, raw memory
  content, raw provider payloads, authorization values, or inline media bodies.

`evaluate_trajectory_improvement_gate(...)` is a manual-review gate, not a
learning loop. A trajectory-derived memory or skill improvement can enter manual
review only when the replay case is redacted and the target regression suite has
passed. The report never permits automatic production policy, memory, skill,
prompt, routing, tool, or provider changes.

## Redaction Rules

Trace and monitoring records must not include:

- API keys, tokens, Authorization headers, cookies, or secret-like strings.
- Raw provider payloads or raw provider responses.
- Full prompts, system messages, or rendered context bodies.
- Hidden chain-of-thought or internal reasoning fields.
- Raw memory content beyond redacted summaries and counters.
- Base64, inline media payloads, full command outputs, or large binary/text blobs.
- Real user data dumps.

Safe trace data includes:

- IDs, statuses, event names, error codes, recoverability, and component names.
- Tool names and prompt-safe input/output summaries.
- Provider/model names when not secret.
- Latency, token counts, retry count, budget summaries, side-effect/risk gate summaries.
- Output references and artifact IDs.
- Redacted context budget and compaction summaries.

Redaction is part of the harness contract. Tests should fail if trace/API output
contains obvious secret or raw payload keys.

## Harness Invariants

Regression tests should enforce these invariants:

- Every `run.started` has exactly one terminal `run.completed`, `run.failed`, or
  `run.cancelled`.
- Every `tool.started` has a matching `tool.finished` or `tool.failed`.
- Every `tool.observation` references a prior tool call or a validation rejection.
- Native provider runtime and mock/offline ReAct runtime both emit
  `react.decision` and terminal run events.
- Successful native provider runtime and mock/offline ReAct runtime both emit
  `response.final` before the terminal run event.
- Native provider runtime emits skipped `memory.save.started` /
  `memory.save.finished` events when automatic task-summary memory is delegated
  to explicit LLM `memory_save` tool calls.
- A validation rejection never enters `ToolExecutor`.
- A failed tool carries error code, source, recovery action, and redacted message.
- Cancel, interrupt, timeout, and hangup traces include their source.
- `/runs/{run_id}` and `/traces/{trace_id}` expose only redacted summaries.
- No public trace, API response, or realtime trace event exposes hidden reasoning.
- Local hook auditing can use `TraceInvariantObserver` to check the first local
  invariant set in-process: run terminal events, tool terminal events,
  `tool.observation` provenance, failed-tool error detail, and hook dispatch
  error redaction.
- Phase 0 trace invariant gate tests live in
  `tests/test_phase0_trace_invariant_gate.py`.
- Current harness development should stop after these invariant tests pass.
  Future work should be driven by a concrete debugging gap rather than adding
  more event types, dashboards, exporters, or debug endpoints preemptively.

## Phase Plan

### Phase 1: Align Trace Semantics

- Add canonical event names to trace summaries without breaking existing public
  response fields.
- Make native provider runtime write `llm.chat`, `react.decision`,
  `action.validation`, `tool.observation`, and terminal run events to
  `TraceStore`.
- Keep existing LangGraph node trace for mock/offline debugging, but map it into
  the canonical timeline.
- Add invariant tests for native and mock/offline runtime paths.

### Phase 2: Developer Trace Viewer

- Add `scripts/trace_view.py`.
- Support lookup by `run_id` or `trace_id`.
- Provide default, `--errors`, and `--json` output modes.
- Keep output redacted and paste-friendly.

### Phase 3: Metrics Summary

- Add a local metrics summary derived from trace/events.
- Expose run/tool/LLM/context/Gateway/memory counters through
  `scripts/trace_metrics.py`.
- Defer debug endpoint exposure until the local metrics contract is stable.
- Keep labels low-cardinality and safe.

### Phase 4: Optional Export

- Add optional OpenTelemetry-compatible export only after local harness semantics
  are stable.
- Export should be disabled by default and must use the same redaction boundary.

### Phase 5: Trajectory Debug Gate

- Build redacted `TrajectoryReplayCase` artifacts from existing trace events.
- Keep replay/eval local and diagnostic-only.
- Require memory or skill regression evidence before any trajectory-derived
  improvement reaches manual review.
- Do not implement RL, automatic self-modification, private-data training, or
  production policy updates.

## Update Rules

- Update this document when run status, trace, event, metrics, Gateway lifecycle
  observability, ReAct trace behavior, or developer trace tooling changes.
- Keep `docs/tool-calling-architecture.md` focused on tool governance and
  lifecycle boundaries; link to this document for observability taxonomy.
- Keep `docs/gateway-architecture.md` focused on Gateway protocol and lifecycle;
  link to this document for cross-runtime trace semantics.
- Do not place current observability architecture only in `docs/development/**`.
  Development plans may reference this document but do not replace it.
