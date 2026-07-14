# Agent-Service Turn Latency Observability Design

## Status

Approved for implementation planning on 2026-07-13.

## Problem

The media-service video understanding MVP can receive H.264 frames, maintain
rolling video memory, let the LLM call `video_understanding`, and return a final
`chatResponse`. The repository already records redacted assistant traces, but
the media entry, background video observation, assistant run, WebSocket send,
and optional delivery ACK do not yet form one developer-readable latency view.

The first polishing phase must make one media conversation turn diagnosable
without adding blocking observability work to its response path. A developer
must be able to identify which stage caused a slow answer and, through an
explicit local debug action, confirm which user/assistant exchange the trace
represents.

## Scope

The first implementation covers only `/agent-service/v1`:

- H.264 video ingestion and selected-keyframe background observation;
- a media `chat` turn entering Gateway and the assistant runtime;
- final `chatResponse` serialization and WebSocket send;
- negotiated `chatResponseAck` delivery confirmation;
- local trace and latency developer tooling.

HTTP, the general Gateway WebSocket, CLI, and `/ws/realtime/media` do not gain
new entry timing in this phase. The implementation reuses canonical trace
contracts so those entry points can adopt the same analysis later.

## Latency Contract

The primary user-facing latency is:

```text
turn_total_ms = chat_response_send_finished - chat_received
```

`chat_response_send_finished` is the return of Starlette/FastAPI WebSocket
`send_text()`. An optional media `chatResponseAck` is a separate delivery
latency and is not part of Agent generation latency.

Durations use `perf_counter_ns()` or an injected equivalent monotonic clock.
UTC timestamps are for display, persistence, and cross-record ordering only.

## Correlation Model

Developer output must distinguish the identifiers already owned by different
layers:

```text
session_turn
chat_index_digest
delivery_id
turn_id
gateway_run_id
assistant_run_id
trace_id
```

The Gateway run ID and Assistant run ID are not interchangeable. `trace_id` is
the canonical cross-surface lookup key. `delivery_id` associates the final
media response and optional ACK. Default trace and log surfaces use a digest
for externally supplied `chatIndex`.

## Architecture

### AgentServiceTurnTiming

One bounded timing record is created for each accepted `delivery_id`. It stores
monotonic checkpoints and correlation identifiers, but no conversation text,
phone number, raw media, prompt, or Provider response.

The record covers:

- accepted/parsed chat;
- time waiting for the per-connection chat run lock;
- Gateway turn start and finish;
- response construction/serialization;
- WebSocket send start and finish;
- optional delivery ACK;
- failure, cancellation, disconnect, or send failure.

The record is connection-owned and removed after terminal delivery handling or
connection cleanup. A delivery without negotiated ACK is terminal after send;
a delivery with negotiated ACK remains until ACK, failure, or disconnect.

### TurnLatencyAnalyzer

The analyzer is a pure service that combines a terminal media timing record
with redacted Assistant trace events. It produces a prompt-safe
`TurnLatencySummary` and does not inspect conversation text.

The critical-path leaf stages are:

```text
entry_parse
chat_queue_wait
conversation_prepare
memory_load
context_build(iteration=N)
llm_chat(iteration=N)
action_validation(iteration=N)
tool_execute(tool=..., call=N)
response_finalize
runtime_postprocess
gateway_overhead
websocket_send
unattributed
```

Parent stages such as total Assistant runtime, realtime backend, or total
Gateway turn are shown as hierarchy but are not ranked against their children.
This prevents double counting and prevents a parent duration from always being
reported as the bottleneck.

For LLM stages, critical-path ranking uses `wall_latency_ms`; reported Provider
latency remains a secondary diagnostic field. Tool stages use ToolExecutor wall
latency. `unattributed_ms` is the non-negative remainder between total latency
and measured, non-overlapping leaf stages.

The bottleneck is the largest critical-path leaf stage. The summary also
contains its duration and percentage of total latency. This phase does not
hard-code a slow-response threshold. After real samples exist, aggregate
p50/p95 values can support a separate SLA decision.

### TurnLatencyReporter

After a successful `send_text()`, the reporter emits one prompt-safe INFO line:

```text
turn_latency status=sent trace=trace_xxx gateway_run=... assistant_run=...
delivery=delivery_xxx session_turn=3 total=2840ms
bottleneck=llm.chat[2] bottleneck_ms=2010 share=70.8%
```

The reporter runs after the primary latency endpoint has been captured. Its
failure must not alter delivery state or close the WebSocket. ACK arrival emits
a separate delivery event or line rather than changing generation latency.

### Trace Composition And Persistence

Synchronous JSONL writes must not be added to the response critical path. The
server trace composition is:

```text
InMemoryTraceStore (immediate current-process queries)
  -> non-blocking bounded persistence queue
  -> background JsonlTraceStore (.data/graph_trace.jsonl)
```

The persistence adapter uses non-blocking enqueue. A full queue increments a
prompt-safe dropped-event counter and may emit a rate-limited warning; it does
not block the assistant. Normal shutdown performs a bounded best-effort flush.
Process crashes may lose the last queued developer trace events.

The first implementation uses a queue capacity of 4096 events and a one-second
shutdown flush limit. These are internal constants in this phase, not new
runtime configuration surface.

Current-process `/runs/{run_id}` and `/traces/{trace_id}` reads use the in-memory
primary. Persisted traces remain inspectable through `trace_view.py --trace-path`
after process exit.

## Video Observation Semantics

Continuous keyframe observation is asynchronous and is not automatically part
of a later chat turn's critical path. The latest rolling snapshot retains only
prompt-safe diagnostic timing required by a consuming turn:

```text
source
snapshot_age_ms
observation_latency_ms
provider
model
pending_count
in_flight
fallback_used
```

When `video_understanding` reads healthy `rolling_video_memory`, the previous
Qwen observation duration is displayed as background context but is not added
to the current turn. When the tool uses `recent_frame_fallback`, its query-time
Provider call is part of `tool_execute` and participates in bottleneck ranking.

H.264 decode, keyframe selection, observer queue wait, Provider analysis, and
snapshot publication may be measured in the background observation record.
Frames are not logged one-by-one at INFO level. Only the latest observation
actually used by a chat turn is projected into that turn's trace.

## Conversation Content Lookup

Default logs, trace events, JSONL persistence, and delivery audit records never
contain user or assistant text. A developer identifies a turn by correlation
IDs and can explicitly request its content:

```bash
python scripts/trace_view.py trace_xxx \
  --server http://127.0.0.1:8000 \
  --include-conversation
```

Conversation content is joined from the existing `ConversationStore` by
`trace_id`; it is not copied into trace storage. Content lookup is disabled by
default, must be explicitly enabled on the server, and is accepted only from a
loopback client. It returns only the current turn's user text and final
assistant response, with bounded lengths and explicit truncation markers. It
does not return prior history, raw media, Provider payloads, memory content, or
hidden reasoning.

The explicit server switch is `--allow-local-trace-content`, backed by
`MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=1` for application-factory configuration.
The guarded read endpoint is `GET /traces/{trace_id}/conversation`. Each user
and assistant field is limited to 1000 Unicode characters; a truncated field
includes `truncated=true` and its original character count.

The CLI refuses `--include-conversation` for a non-loopback server URL. Returned
content is written only to the current terminal and is never written back to a
trace or delivery record.

## Failure Semantics

Observability remains observer-only:

- timing, analysis, persistence, or reporting failures cannot fail a turn;
- cancellation, timeout, Provider failure, and send failure produce a partial
  summary with terminal status and the last completed stage;
- a failed WebSocket send is not reported as `status=sent`;
- ACK not negotiated is `not_negotiated`;
- negotiated but not yet received ACK is `pending`;
- a received ACK creates a separate terminal delivery timing event;
- disconnect records distinguish before-send and before-ACK states;
- trace queue overflow drops persistence work rather than blocking response;
- cleanup removes per-delivery timing state and does not leak conversation
  content into errors.

## Developer Surfaces

The default local workflow is:

```bash
python scripts/run_server.py
python scripts/trace_view.py <trace_id> --server http://127.0.0.1:8000
```

The default trace view displays identifiers, total latency, bottleneck,
bottleneck share, stage hierarchy, video source/freshness, errors, and
unattributed time. `--json` exposes the same prompt-safe structure for tests or
later dashboards. `--include-conversation` is a separate explicit debug path.

## TDD And Acceptance Criteria

Implementation proceeds test-first and must cover:

1. Stage durations, nested-stage exclusion, and `unattributed_ms` calculation.
2. Parameterized injected delays that select each expected bottleneck.
3. LLM wall latency versus secondary Provider latency.
4. Background rolling-memory observation excluded from the critical path.
5. Query-time recent-frame fallback included in tool latency.
6. WebSocket receive, queue, Gateway, response send, and ACK timing.
7. Two same-session chats where the second records actual lock queue wait.
8. Partial summaries for cancellation, Provider failure, timeout, send failure,
   disconnect-before-send, and disconnect-before-ACK.
9. Observer, reporter, and persistence writer failures not affecting response.
10. Ordinary logs, traces, and JSONL containing no conversation text, phone
    number, raw media, absolute frame path, or Provider raw response.
11. Explicit loopback content lookup returning only the matching trace's
    bounded current-turn content.
12. Existing Gateway, agent-service, realtime video memory, trace, redaction,
    and trace-view regression suites.
13. A local mock WebSocket smoke where the summary identifies the injected
    slow stage and `trace_view.py` reconstructs the same turn.

The real-time protection acceptance condition is structural: no file I/O,
content lookup, aggregate trace analysis, or log formatting occurs before the
captured `send_text()` endpoint except constant-size in-memory timing and trace
append operations already required by the runtime.

## Non-Goals

This phase does not add:

- an external OpenTelemetry collector or APM dependency;
- a dashboard or developer web UI;
- raw prompt, response, media, or Provider payload tracing;
- a fixed production latency SLA;
- per-frame INFO logging;
- timing changes to non-agent-service entry points;
- changes to LLM tool-selection behavior or video understanding semantics.
