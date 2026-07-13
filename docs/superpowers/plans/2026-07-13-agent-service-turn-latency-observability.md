# Agent-Service Turn Latency Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every `/agent-service/v1` chat turn report a prompt-safe end-to-end latency breakdown and bottleneck, with correlated video snapshot diagnostics, non-blocking persistence, optional ACK timing, and an explicitly gated local view of the matching conversation content.

**Architecture:** Keep constant-size timing state per accepted media delivery, then merge its terminal checkpoints with the existing redacted Assistant `TraceEvent` timeline after `send_text()` returns. Use an in-memory primary trace store and a bounded background JSONL writer so observability never adds file I/O to the response path. Keep conversation text out of logs and traces; an explicit loopback-only debug endpoint joins the existing `ConversationStore` by `trace_id`.

**Tech Stack:** Python 3.11+, FastAPI/Starlette WebSocket, Pydantic v2, asyncio, standard-library `queue`/`threading`, pytest, existing `TraceStore`, `ConversationStore`, Gateway, and realtime video memory services.

## Global Constraints

- Scope is `/agent-service/v1`; do not add entry timing to HTTP, general Gateway WebSocket, CLI, or `/ws/realtime/media`.
- Primary latency is `chat_response_send_finished - chat_received`; ACK latency is separate.
- Durations use a monotonic nanosecond clock; UTC timestamps are display/persistence data only.
- Distinguish `gateway_run_id`, `assistant_run_id`, and `trace_id`; never treat the two run IDs as interchangeable.
- Logs, trace events, JSONL, and delivery audit must not contain conversation text, phone numbers, raw media, absolute frame paths, Provider payloads, or hidden reasoning.
- Do not put synchronous file I/O, content lookup, aggregate trace analysis, or log formatting before the captured `send_text()` endpoint.
- A full persistence queue drops the persistence event and increments a safe counter; it never blocks a response.
- Queue capacity is 4096 events and shutdown flush timeout is one second.
- Local conversation content is disabled by default, limited to 1000 Unicode characters per side, and available only through an explicit loopback debug path.
- Do not install new dependencies or call real external Providers; use mock/scripted adapters and local tests.
- Preserve unrelated dirty worktree changes. Stage only the exact files listed in each commit step.

## File Map

**Create:**

- `src/assistant_agent/services/agent_service_latency.py`: per-delivery timing, stage models, bottleneck analysis, safe trace append, and log reporting.
- `src/assistant_agent/services/trace_persistence.py`: bounded background JSONL writer and server trace composition.
- `src/assistant_agent/services/trace_conversation.py`: bounded current-turn lookup from `ConversationStore`.
- `tests/test_agent_service_latency.py`, `tests/test_trace_persistence.py`, `tests/test_trace_conversation.py`.

**Modify:**

- Runtime timing: `src/assistant_agent/services/assistant_run_service.py`, `src/assistant_agent/agent/runtime.py`, `src/assistant_agent/agent/tool_scheduler.py`, `src/assistant_agent/agent/graph_nodes.py`, `src/assistant_agent/services/response_observability.py`.
- Video timing: `src/assistant_agent/services/realtime_video_memory.py`, `src/assistant_agent/services/realtime_video_observer.py`, `src/assistant_agent/tools/video_tool.py`.
- Media/trace entry: `src/assistant_agent/api/agent_service_websocket.py`, `src/assistant_agent/services/agent_service_delivery.py`, `src/assistant_agent/services/trace_query.py`.
- Persistence/lifecycle: `src/assistant_agent/services/trace_store.py`, `src/assistant_agent/api/routes_agent.py`, `src/assistant_agent/api/app.py`, `scripts/run_server.py`.
- Debug content/view: `src/assistant_agent/api/routes_agent.py`, `scripts/run_server.py`, `scripts/trace_view.py`.
- Tests and docs named in the tasks below.

---

### Task 1: Core Turn Timing And Bottleneck Analyzer

**Files:**
- Create: `src/assistant_agent/services/agent_service_latency.py`
- Create: `tests/test_agent_service_latency.py`
- Include in commit: `docs/superpowers/specs/2026-07-13-agent-service-turn-latency-observability-design.md`
- Include in commit: `docs/superpowers/plans/2026-07-13-agent-service-turn-latency-observability.md`

**Interfaces:**
- Consumes: `assistant_agent.services.trace_store.TraceEvent`.
- Produces: `AgentServiceTurnTiming.mark()`, `AgentServiceTurnTiming.bind_turn()`, `TurnLatencyStage`, `VideoLatencyContext`, `TurnLatencySummary`, `analyze_agent_service_turn()`, `append_turn_latency_trace()`, and `report_turn_latency()`.

- [ ] **Step 1: Write failing deterministic transport tests**

```python
def test_turn_timing_computes_transport_durations_without_content() -> None:
    timing = AgentServiceTurnTiming(
        delivery_id="delivery_1",
        session_turn=3,
        chat_index_digest="digest_1",
        expects_ack=True,
        received_ns=1_000_000_000,
        accepted_ns=1_004_000_000,
    )
    timing.mark("queue_entered", at_ns=1_005_000_000)
    timing.mark("queue_acquired", at_ns=1_012_000_000)
    timing.mark("gateway_started", at_ns=1_013_000_000)
    timing.mark("gateway_finished", at_ns=1_080_000_000)
    timing.mark("send_started", at_ns=1_081_000_000)
    timing.mark("send_finished", at_ns=1_084_000_000)

    summary = analyze_agent_service_turn(timing, [], status="sent")

    assert summary.total_ms == 84
    assert summary.stage("entry_parse").duration_ms == 4
    assert summary.stage("chat_queue_wait").duration_ms == 7
    assert summary.stage("websocket_send").duration_ms == 3
    assert "speech" not in summary.model_dump_json()
```

- [ ] **Step 2: Run the transport test and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_latency.py::test_turn_timing_computes_transport_durations_without_content -q
```

Expected: import fails because `agent_service_latency` does not exist.

- [ ] **Step 3: Write failing trace-stage and bottleneck tests**

Create safe terminal `TraceEvent` fixtures for memory, context, LLM, validation, tool, response, and postprocess. Parameterize the largest stage. Include:

```python
def test_llm_bottleneck_uses_wall_latency_not_provider_latency() -> None:
    events = [
        _event(
            "llm.chat.finished",
            latency_ms=30,
            attributes={"iteration": 2, "provider_latency_ms": 30, "wall_latency_ms": 90},
        )
    ]
    summary = analyze_agent_service_turn(_sent_timing(total_ms=100), events, status="sent")
    stage = summary.stage("llm_chat[2]")
    assert stage.duration_ms == 90
    assert stage.provider_latency_ms == 30
    assert summary.bottleneck == "llm_chat[2]"
```

- [ ] **Step 4: Run the analyzer file and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_latency.py -q
```

Expected: timing models and analyzer are missing.

- [ ] **Step 5: Implement the public timing and summary shapes**

```python
CheckpointName = Literal[
    "queue_entered", "queue_acquired", "gateway_started", "gateway_finished",
    "response_built", "send_started", "send_finished", "ack_received",
    "failed", "disconnected",
]

@dataclass
class AgentServiceTurnTiming:
    delivery_id: str
    session_turn: int
    chat_index_digest: str
    expects_ack: bool
    received_ns: int
    accepted_ns: int
    checkpoints: dict[str, int] = field(default_factory=dict)
    turn_id: str | None = None
    gateway_run_id: str | None = None
    assistant_run_id: str | None = None
    trace_id: str | None = None

    def mark(self, name: CheckpointName, *, at_ns: int | None = None) -> None:
        self.checkpoints[name] = perf_counter_ns() if at_ns is None else at_ns

    def bind_turn(self, *, turn_id: str | None, gateway_run_id: str | None,
                  assistant_run_id: str | None, trace_id: str | None) -> None:
        self.turn_id = turn_id
        self.gateway_run_id = gateway_run_id
        self.assistant_run_id = assistant_run_id
        self.trace_id = trace_id

class TurnLatencyStage(BaseModel):
    name: str
    duration_ms: int = Field(ge=0)
    critical_path: bool = True
    iteration: int | None = None
    tool_name: str | None = None
    provider: str | None = None
    model: str | None = None
    provider_latency_ms: int | None = Field(default=None, ge=0)

class VideoLatencyContext(BaseModel):
    source: str | None = None
    snapshot_age_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    pending_count: int | None = Field(default=None, ge=0)
    in_flight: bool | None = None
    fallback_used: bool = False
    snapshot_sequence: int | None = Field(default=None, ge=0)

class TurnLatencySummary(BaseModel):
    schema_version: Literal["agent_service_turn_latency_v1"] = "agent_service_turn_latency_v1"
    status: str
    delivery_id: str
    session_turn: int
    chat_index_digest: str
    turn_id: str | None = None
    gateway_run_id: str | None = None
    assistant_run_id: str | None = None
    trace_id: str | None = None
    total_ms: int | None = Field(default=None, ge=0)
    stages: list[TurnLatencyStage] = Field(default_factory=list)
    bottleneck: str | None = None
    bottleneck_ms: int | None = Field(default=None, ge=0)
    bottleneck_share_pct: float | None = Field(default=None, ge=0, le=100)
    unattributed_ms: int | None = Field(default=None, ge=0)
    ack_status: Literal["not_negotiated", "pending", "acked"]
    ack_latency_ms: int | None = Field(default=None, ge=0)
    terminal_stage: str | None = None
    video: VideoLatencyContext | None = None
```

Add `stage(name)` to return the named stage or raise `KeyError`.

- [ ] **Step 6: Implement leaf extraction and bottleneck calculation**

Map terminal canonical events exactly:

```python
TRACE_STAGE_EVENTS = {
    "conversation.prepare.finished": "conversation_prepare",
    "memory.load.finished": "memory_load",
    "context.build.finished": "context_build",
    "llm.chat.finished": "llm_chat",
    "action.validation.finished": "action_validation",
    "tool.finished": "tool_execute",
    "tool.failed": "tool_execute",
    "response.final": "response_finalize",
    "runtime.postprocess.finished": "runtime_postprocess",
}
```

Suffix repeated trace stages with iteration and tools with tool name. Prefer LLM `wall_latency_ms`; preserve Provider latency separately. Compute `gateway_overhead` from Gateway total minus realtime backend latency, clamp all derived values to zero, add positive `unattributed_ms`, and rank only leaf stages marked `critical_path=True`.

Read the latest `video_understanding` tool terminal event's sanitized output summary into `VideoLatencyContext`. Treat rolling observation latency and snapshot age as non-critical diagnostics; only query-time fallback remains represented in the critical `tool_execute[video_understanding]` wall latency. Add tests for both rolling-memory and fallback summaries.

- [ ] **Step 7: Implement safe trace append and one-line reporting**

Append `agent_service.turn.finished` only when trace and Assistant run IDs exist. Store the model under `output_summary["turn_latency"]`. Reporter output contains only status, trace, both run IDs, delivery, session turn, total, bottleneck, bottleneck milliseconds, and percentage; catch logger exceptions.

- [ ] **Step 8: Run unit tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_latency.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit core analysis with approved docs**

```bash
git add src/assistant_agent/services/agent_service_latency.py tests/test_agent_service_latency.py docs/superpowers/specs/2026-07-13-agent-service-turn-latency-observability-design.md docs/superpowers/plans/2026-07-13-agent-service-turn-latency-observability.md
git commit -m "Add agent service latency analysis"
```

---

### Task 2: Complete Assistant Critical-Path Trace Fields

**Files:**
- Modify: `src/assistant_agent/services/assistant_run_service.py:423-541,873-952`
- Modify: `src/assistant_agent/agent/runtime.py:168-365,439-477,735-805,1103-1201`
- Modify: `src/assistant_agent/agent/tool_scheduler.py:19-135`
- Modify: `src/assistant_agent/agent/graph_nodes.py:253-269`
- Modify: `src/assistant_agent/services/response_observability.py:11-55`
- Test: `tests/test_shared_assistant_run_service.py`
- Test: `tests/test_observability_harness.py`

**Interfaces:**
- Consumes: existing `TraceStore` and canonical trace helpers.
- Produces: `conversation.prepare.finished`, validation `latency_ms`, and response-finalization `latency_ms`.

- [ ] **Step 1: Write a failing preparation trace test**

Run one request with `AgentGraphRuntime(trace_store=InMemoryTraceStore())`. Assert exactly one `conversation.prepare.finished` appears before `run.started`, has non-negative latency, and contains no request text.

- [ ] **Step 2: Run the preparation test and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_shared_assistant_run_service.py -k conversation_prepare_trace -q
```

Expected: the event is absent.

- [ ] **Step 3: Measure preparation and emit it at runtime entry**

Measure `_prepare_conversation_request()` plus `prepare_realtime_task_state_request()` in `run_assistant_request()`. Copy only the integer into `resolved_request.metadata["conversation_prepare_latency_ms"]`. In `run_state()`, append before `run.started`:

```python
prepare_latency = state.request.metadata.get("conversation_prepare_latency_ms")
if isinstance(prepare_latency, int) and prepare_latency >= 0:
    self._append_observability_event(
        state,
        canonical_event="conversation.prepare.finished",
        status="succeeded",
        latency_ms=prepare_latency,
        attributes={"conversation_turn_index": state.request.metadata.get("conversation_turn_index")},
    )
```

- [ ] **Step 4: Write failing native validation/response latency tests**

Use the scripted real-chat adapter pattern: one `video_understanding` call followed by a final answer. Assert terminal validation and `response.final` events both have non-negative latency.

- [ ] **Step 5: Run native timing tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py -k "validation_latency or response_finalization_latency" -q
```

Expected: latency fields are `None`.

- [ ] **Step 6: Measure validation without changing decisions**

Add `validation_latency_ms: int = 0` to `ScheduledToolCall` and `build_scheduled_tool_call()`. Measure schedule validation and serial revalidation with `perf_counter()`, then set the emitted `action.validation.finished.latency_ms`. Preserve schedule order, rejection, and parallel safety behavior.

- [ ] **Step 7: Measure response finalization**

Add `latency_ms` to `append_response_final_event()`. Start timing at native `_set_native_runtime_response()` entry and pass elapsed latency on success/failure. In `compose_response_node()`, measure `compose_response()`. Do not modify the user's dirty `assistant_loop_nodes.py`.

- [ ] **Step 8: Run focused runtime tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_shared_assistant_run_service.py tests/test_observability_harness.py tests/test_native_tool_call_handoff.py -q
```

Expected: all pass.

- [ ] **Step 9: Commit runtime timing**

```bash
git add src/assistant_agent/services/assistant_run_service.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/tool_scheduler.py src/assistant_agent/agent/graph_nodes.py src/assistant_agent/services/response_observability.py tests/test_shared_assistant_run_service.py tests/test_observability_harness.py
git commit -m "Trace assistant critical path latency"
```

---

### Task 3: Add Rolling Video Observation Diagnostics

**Files:**
- Modify: `src/assistant_agent/services/realtime_video_memory.py:12-130`
- Modify: `src/assistant_agent/services/realtime_video_observer.py:25-230`
- Modify: `src/assistant_agent/tools/video_tool.py:34-128`
- Test: `tests/test_realtime_video_memory.py`
- Test: `tests/test_realtime_video_observer.py`
- Test: `tests/test_video_understanding_tool.py`

**Interfaces:**
- Consumes: selected keyframes and existing observation `ToolResult`.
- Produces: `RealtimeVideoObservationDiagnostics`, snapshot diagnostics, and safe `ToolResult.trace_summary` fields.

- [ ] **Step 1: Write failing snapshot diagnostics tests**

```python
diagnostics = RealtimeVideoObservationDiagnostics(
    h264_decode_latency_ms=4,
    keyframe_selection_latency_ms=2,
    queue_wait_latency_ms=7,
    observation_latency_ms=80,
    published_at_ms=10_000,
)
store.record_success("video-a", frame, result, diagnostics=diagnostics)
assert store.snapshot("video-a").observation_diagnostics == diagnostics
```

- [ ] **Step 2: Run the memory test and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_memory.py -k diagnostics -q
```

Expected: model and argument are missing.

- [ ] **Step 3: Add the immutable diagnostics model**

```python
class RealtimeVideoObservationDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)
    h264_decode_latency_ms: int | None = Field(default=None, ge=0)
    keyframe_selection_latency_ms: int | None = Field(default=None, ge=0)
    queue_wait_latency_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    published_at_ms: int | None = Field(default=None, ge=0)
```

Add `observation_diagnostics` to `RealtimeVideoSnapshot`; extend `record_success(..., diagnostics=None)` and preserve the last successful value across pending/failure updates.

- [ ] **Step 4: Write failing observer timing tests**

Inject `clock_ns` and `wall_clock_ms` callables. Use deterministic values to assert selection, queue wait, observation, and publication durations while preserving one-inflight/latest-pending behavior.

- [ ] **Step 5: Run observer timing tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_observer.py -k timing -q
```

Expected: constructor and queue record do not support timing.

- [ ] **Step 6: Measure observer phases**

Add frozen internal `_QueuedObservation(record, enqueued_ns, h264_decode_latency_ms, keyframe_selection_latency_ms)`. Change the bounded queue to this type, measure queue wait and complete observation execution, and pass diagnostics on success. Keep monotonic timestamps out of the public snapshot.

- [ ] **Step 7: Write failing video-tool trace tests**

Assert healthy memory returns safe trace summary fields `source`, `snapshot_age_ms`, `observation_latency_ms`, `pending_count`, `in_flight`, `fallback_used=False`, and `snapshot_sequence`. Assert query-time Provider use sets `source=recent_frame_fallback` and `fallback_used=True`. Inject a wall clock into the tool.

- [ ] **Step 8: Project safe diagnostics through `ToolResult.trace_summary`**

Keep user-facing result data unchanged. Do not include frame paths, fingerprints, semantic response text, or raw media timestamps. Rely on ToolExecutor's existing sanitized trace-summary projection.

- [ ] **Step 9: Run video tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_video_understanding_tool.py tests/test_video_context.py -q
```

Expected: all pass.

- [ ] **Step 10: Commit video diagnostics**

```bash
git add src/assistant_agent/services/realtime_video_memory.py src/assistant_agent/services/realtime_video_observer.py src/assistant_agent/tools/video_tool.py tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_video_understanding_tool.py
git commit -m "Trace rolling video observation latency"
```

---

### Task 4: Integrate Media WebSocket Turn Timing And ACK Reporting

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py:35-67,298-340,364-481,589-884`
- Modify: `src/assistant_agent/services/agent_service_delivery.py:26-163`
- Modify: `src/assistant_agent/services/trace_query.py:15-104`
- Test: `tests/test_agent_service_websocket.py`
- Test: `tests/test_agent_service_delivery.py`
- Test: `tests/test_trace_query_api.py`

**Interfaces:**
- Consumes: Task 1 timing/analyzer/reporting and Task 3 video diagnostics.
- Produces: `agent_service.turn.finished`, safe `turn_latency` INFO output, and separate `agent_service.delivery.acked` timing.

- [ ] **Step 1: Write a failing WebSocket correlation test**

Use an actual `AgentGraphRuntime(trace_store=InMemoryTraceStore())` with mock Providers. Send a media chat and inspect the matching trace after receiving `chatResponse`. Assert total/send latency, distinct Gateway/Assistant run IDs, matching trace ID, and absence of the utterance/phone number.

- [ ] **Step 2: Run the correlation test and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -k turn_latency_trace -q
```

Expected: no agent-service terminal latency event exists.

- [ ] **Step 3: Add connection-owned timing state**

```python
turn_timings: dict[str, AgentServiceTurnTiming] = field(default_factory=dict)
session_turn_counter: int = 0
clock_ns: Callable[[], int] = perf_counter_ns
```

Extend `PreparedChat` with `received_ns`, `accepted_ns`, and `session_turn`. Capture receive time immediately after `receive_text()`. Increment the turn counter only after chat validation. Create timing immediately after `delivery_registry.accept()` using `delivery.chat_index_digest`.

- [ ] **Step 4: Measure the transport checkpoints**

Mark, in order:

```text
queue_entered
queue_acquired
gateway_started
gateway_finished
response_built
send_started
send_finished
```

After Gateway returns, bind `turn_id`, Gateway run ID, trace ID, and Assistant run ID resolved from the first trace event. After `_send_response()` returns, mark sent, analyze, append trace summary, and report inside an observer-only exception guard. Remove a non-ACK timing record after reporting.

- [ ] **Step 5: Measure H.264 decode before observer submission**

Measure `video_ingestion.ingest()` around `asyncio.to_thread()`. Use `dataclasses.replace()` to put `h264_decode_latency_ms` in frozen `VideoFrame.metadata`, then submit the frame. Do not log each frame at INFO.

- [ ] **Step 6: Write failing ACK/failure/disconnect tests**

Negotiate ACK, send it, and assert a separate event with non-negative ACK latency. Add no-negotiation, disconnect-before-send, disconnect-before-ACK, and failing-send cases. A send failure must not report `status=sent`.

- [ ] **Step 7: Implement terminal ACK and failure handling**

On ACK, retrieve timing, mark `ack_received`, append `agent_service.delivery.acked`, emit an ACK-only safe line, and remove timing. On cleanup, mark remaining records disconnected, append partial summaries when trace correlation exists, and clear them. `AgentServiceDeliveryRegistry` remains delivery-state authority; audit metadata stays prompt-safe.

- [ ] **Step 8: Add same-session queue-wait coverage**

Use a blocking fake Gateway facade. Send two chats without waiting for the first result, release the first, and assert the second has positive `chat_queue_wait`; inject checkpoint values so it is the reported bottleneck. Confirm intervening video ACK remains responsive.

- [ ] **Step 9: Project the latest summary through trace queries**

Add `turn_latency: dict[str, Any] | None` to `RunSummary` and `TraceSummary`. Read only the last `output_summary["turn_latency"]` with schema `agent_service_turn_latency_v1`. Test that APIs expose it without conversation content.

- [ ] **Step 10: Verify observer failures and log redaction**

Monkeypatch analyzer, trace append, and reporter to raise separately; every case must still receive `chatResponse`. With `caplog`, assert the safe line contains trace/delivery IDs but not user text, answer text, phone number, or raw chat index.

- [ ] **Step 11: Run media tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py tests/test_trace_query_api.py -q
```

Expected: all pass.

- [ ] **Step 12: Commit media integration**

```bash
git add src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/services/agent_service_delivery.py src/assistant_agent/services/trace_query.py tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py tests/test_trace_query_api.py
git commit -m "Report agent service turn latency"
```

---

### Task 5: Add Non-Blocking Server Trace Persistence

**Files:**
- Create: `src/assistant_agent/services/trace_persistence.py`
- Create: `tests/test_trace_persistence.py`
- Modify: `src/assistant_agent/services/trace_store.py:66-275`
- Modify: `src/assistant_agent/services/assistant_run_service.py:423-432`
- Modify: `src/assistant_agent/api/routes_agent.py:88-105`
- Modify: `src/assistant_agent/api/app.py:57-64`
- Modify: `scripts/run_server.py:38-100,180-222`
- Test: `tests/test_run_server.py`

**Interfaces:**
- Consumes: `JsonlTraceStore`, `InMemoryTraceStore`, and `CompositeTraceStore`.
- Produces: `BufferedJsonlTraceStore`, `create_server_trace_store()`, `close_trace_store()`, and optional `trace_store` injection into `create_runtime()`.

- [ ] **Step 1: Write failing queue/flush/drop tests**

```python
primary = InMemoryTraceStore()
secondary = BufferedJsonlTraceStore(JsonlTraceStore(path), capacity=1)
store = CompositeTraceStore(primary, [secondary])
store.append(_trace_event("run_1"))
assert primary.list_by_run("run_1")
assert secondary.flush(timeout=1.0) is True
assert JsonlTraceStore(path).list_by_run("run_1")
```

Use a blocking fake sink to fill capacity; a second append must return promptly and set `dropped_event_count == 1`. Cover close and delete-by-user.

- [ ] **Step 2: Run persistence tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_persistence.py -q
```

Expected: buffered store is missing.

- [ ] **Step 3: Implement the bounded writer**

Use `queue.Queue(maxsize=4096)` and one daemon thread. `append()` uses `put_nowait(("append", event))`; `queue.Full` only increments a locked counter. `flush(timeout)` enqueues a barrier carrying `threading.Event`. `delete_by_user()` enqueues a synchronous delete command. `close(timeout=1.0)` flushes, sends stop, and joins within the deadline.

- [ ] **Step 4: Add composite close support**

Add `close(timeout=1.0)` to `CompositeTraceStore`. Close secondaries before primary, collect failures with the existing hook-dispatch mechanism, and preserve `continue_on_error`. Do not alter append/read/delete behavior.

- [ ] **Step 5: Write failing server wiring tests**

Assert injected trace store identity in `create_runtime()`, `run_server.py` setting `MULTIMODAL_AGENT_SERVER_TRACE_ENABLED=1`, conditional server-store creation in routes, and application shutdown close.

- [ ] **Step 6: Wire persistence only for `run_server.py`**

Add `trace_store` to `create_runtime()`. Set the server trace flag in `_prepare_environment()`. `routes_agent` lazily creates the composite only when enabled; tests/direct library calls keep the current in-memory default. Add `shutdown_agent_runtime()` and invoke it after Gateway shutdown.

- [ ] **Step 7: Verify redaction and response-path isolation**

Persist fake phone/prompt/path/Provider fields and assert they are absent from JSONL. In a WebSocket test, block the JSONL sink and assert `chatResponse` arrives before release.

- [ ] **Step 8: Run persistence/server tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_persistence.py tests/test_run_server.py tests/test_agent_service_websocket.py -q
```

Expected: all pass and close tests leave no writer thread alive.

- [ ] **Step 9: Commit persistence**

```bash
git add src/assistant_agent/services/trace_persistence.py src/assistant_agent/services/trace_store.py src/assistant_agent/services/assistant_run_service.py src/assistant_agent/api/routes_agent.py src/assistant_agent/api/app.py scripts/run_server.py tests/test_trace_persistence.py tests/test_run_server.py tests/test_agent_service_websocket.py
git commit -m "Persist traces off the response path"
```

---

### Task 6: Add Explicit Loopback Conversation Lookup And Trace View

**Files:**
- Create: `src/assistant_agent/services/trace_conversation.py`
- Create: `tests/test_trace_conversation.py`
- Modify: `src/assistant_agent/api/routes_agent.py:312-365`
- Modify: `scripts/run_server.py:41-100,126-163`
- Modify: `scripts/trace_view.py:23-250`
- Test: `tests/test_trace_query_api.py`
- Test: `tests/test_trace_view_script.py`
- Test: `tests/test_run_server.py`

**Interfaces:**
- Consumes: `ConversationStore.get()`, trace identity events, and Task 4 correlation.
- Produces: `TraceConversationView`, `find_trace_conversation()`, guarded `GET /traces/{trace_id}/conversation`, `--allow-local-trace-content`, and `--include-conversation`.

- [ ] **Step 1: Write failing bounded lookup tests**

Use two turns in `InMemoryConversationStore`. Select only the matching trace, clip each side to 1000 Unicode characters, record original length/truncation, and return no history or identity.

- [ ] **Step 2: Run content tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_conversation.py -q
```

Expected: service/model are absent.

- [ ] **Step 3: Implement bounded content models**

```python
class TraceConversationText(BaseModel):
    text: str
    chars: int = Field(ge=0)
    truncated: bool = False

class TraceConversationView(BaseModel):
    schema_version: Literal["trace_conversation_view_v1"] = "trace_conversation_view_v1"
    trace_id: str
    user: TraceConversationText
    assistant: TraceConversationText

def find_trace_conversation(store: ConversationStore, *, user_id: str,
                            session_id: str, trace_id: str,
                            limit: int = 1000) -> TraceConversationView | None:
```

Use character slicing; do not include prior turns.

- [ ] **Step 4: Write failing endpoint gate tests**

Assert: flag absent returns 404; enabled non-loopback returns 403; enabled loopback returns matching bounded turn; unknown trace/content returns 404. Use `TestClient(create_app(), client=("127.0.0.1", 50000))` for allowed access.

- [ ] **Step 5: Implement the guarded endpoint**

Enable only with `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=1`. Check `request.client.host` using `ipaddress.ip_address(host).is_loopback`, accepting literal `localhost`. Read internal trace identity, query the configured conversation store, and never return identity fields. Return 404 when disabled.

- [ ] **Step 6: Add `run_server.py` content flag**

Add `--allow-local-trace-content`; when present set the environment variable. Otherwise preserve any explicit existing environment setting. Print only `local_trace_content: enabled|disabled`.

- [ ] **Step 7: Write failing trace-view latency and conversation CLI tests**

Serve trace and conversation routes locally. Without the content flag, assert human output renders `turn_latency` identifiers, total, bottleneck, critical leaf stages, ACK state, and video diagnostics before the timeline; assert JSON preserves `turn_latency` as structured data. With the flag, assert human output also contains a `Conversation` section with the current user/assistant text and JSON nests content under `conversation`. Assert the flag without `--server` fails and a non-loopback URL is rejected before network I/O.

- [ ] **Step 8: Implement latency rendering and loopback-only content fetch**

Render the Task 4 top-level `turn_latency` summary for both run and trace lookups, including an explicit `unattributed` row when positive. Validate hostname with `ipaddress`, resolve the trace ID from run/trace payload, fetch the guarded endpoint, and render conversation after latency summary but before timeline. Preserve the existing timeline when no latency summary exists, and preserve existing output without the content flag.

- [ ] **Step 9: Run content/CLI tests and verify GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_conversation.py tests/test_trace_query_api.py tests/test_trace_view_script.py tests/test_run_server.py -q
```

Expected: all pass.

- [ ] **Step 10: Commit content tooling**

```bash
git add src/assistant_agent/services/trace_conversation.py src/assistant_agent/api/routes_agent.py scripts/run_server.py scripts/trace_view.py tests/test_trace_conversation.py tests/test_trace_query_api.py tests/test_trace_view_script.py tests/test_run_server.py
git commit -m "Add gated trace conversation lookup"
```

---

### Task 7: Documentation, Mock Smoke, And Full Verification

**Files:**
- Modify: `docs/observability-harness.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify if implementation review requires clarification: `docs/superpowers/specs/2026-07-13-agent-service-turn-latency-observability-design.md`
- Modify if implementation review requires clarification: `docs/superpowers/plans/2026-07-13-agent-service-turn-latency-observability.md`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: operator workflow, mock smoke evidence, and final verified commits.

- [ ] **Step 1: Update observability authority**

Document send boundary, separate ACK, identifier distinction, stage hierarchy, bottleneck rule, background video semantics, non-blocking persistence, safe log example, and exact trace-view commands. Preserve the rule that `videoResponse(code=0)` is ingestion evidence, not MLLM completion.

- [ ] **Step 2: Update media WebSocket runbook**

Add this symptom mapping:

```text
chat_queue_wait           previous turn still active
conversation_prepare      history/context preparation
llm_chat[1]               slow tool-selection model call
tool_execute[video...]    query-time fallback or Provider delay
llm_chat[2]               slow final-answer model call
websocket_send            transport backpressure
ACK pending               delivery confirmation missing
snapshot_age high         background observation stale
```

Include safe lookup commands and the explicit local-content warning.

- [ ] **Step 3: Run mock WebSocket smoke tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -k "turn_latency or queue_wait or ack" -q -s
```

Expected: PASS; capture shows one safe latency line and the injected slow stage as bottleneck.

- [ ] **Step 4: Run the focused regression set**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_latency.py tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py tests/test_trace_persistence.py tests/test_trace_conversation.py tests/test_trace_query_api.py tests/test_trace_view_script.py tests/test_run_server.py tests/test_observability_harness.py tests/test_shared_assistant_run_service.py tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_video_understanding_tool.py tests/test_video_context.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py -q
```

Expected: all pass.

- [ ] **Step 5: Run environment and fast-suite verification**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: environment check and fast suite pass.

- [ ] **Step 6: Run lint and diff checks**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src/assistant_agent/services/agent_service_latency.py src/assistant_agent/services/trace_persistence.py src/assistant_agent/services/trace_conversation.py src/assistant_agent/services/assistant_run_service.py src/assistant_agent/services/response_observability.py src/assistant_agent/services/realtime_video_memory.py src/assistant_agent/services/realtime_video_observer.py src/assistant_agent/services/trace_query.py src/assistant_agent/services/trace_store.py src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/api/routes_agent.py src/assistant_agent/api/app.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/tool_scheduler.py src/assistant_agent/agent/graph_nodes.py src/assistant_agent/tools/video_tool.py scripts/run_server.py scripts/trace_view.py tests/test_agent_service_latency.py tests/test_trace_persistence.py tests/test_trace_conversation.py
git diff --check
```

Expected: both pass.

- [ ] **Step 7: Inspect redaction evidence**

Search generated `.data/graph_trace.jsonl` and captured INFO output for test utterance, answer, fake phone, absolute JPEG path, and media payload. Every search returns no match. Remove temporary smoke trace; do not commit generated JSONL.

- [ ] **Step 8: Commit final docs/adjustments**

```bash
git add docs/observability-harness.md docs/media-agent-service-websocket.md docs/superpowers/specs/2026-07-13-agent-service-turn-latency-observability-design.md docs/superpowers/plans/2026-07-13-agent-service-turn-latency-observability.md
git commit -m "Document media turn latency diagnostics"
```

- [ ] **Step 9: Inspect final history and worktree scope**

```bash
git log --oneline -7
git status --short
```

Expected: feature commits are present; unrelated context/tool changes remain untouched; no trace JSONL, media artifact, key, or Provider response is staged.

## Execution Notes

- Create a dedicated worktree at execution time because the primary worktree contains unrelated user changes.
- Task 1 intentionally commits the approved design and plan with implementation, satisfying the repository rule against standalone design commits.
- Do not run real Qwen/DeepSeek calls unless the user separately authorizes a new `provider_smoke` run.
- Reproduce unrelated full-suite failures on the base branch before changing scope; do not repair unrelated calendar/context/tool behavior here.
