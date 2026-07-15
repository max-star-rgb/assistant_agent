# Agent-Service Streaming and Video Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Honor Media `chat.stream=true` end to end, make realtime visual replies sound natural, and prevent current-camera questions from silently consuming older semantic snapshots.

**Architecture:** Keep App → Media → `/agent-service/v1` → Gateway → AgentGraphRuntime as the only product path. Add an optional stream consumer to `GatewayTurnFacade`, translate committed Gateway chunks into Media-compatible `chatResponse` deltas, and add a sequence-based freshness barrier to the existing single-flight realtime video observer. Preserve final response/ACK behavior and non-realtime upload behavior.

**Tech Stack:** Python 3.11+, FastAPI WebSocket, asyncio, Pydantic v2, pytest, existing Gateway/runtime event stream and realtime video observer.

## Global Constraints

- Do not change the App protocol or make App connect directly to Agent.
- Keep Media `message` plus stringified `body` envelopes and existing `chatResponse` message type.
- Intermediate stream packets use delta text with `status=PROCESSING`, `sequence>=1`, and `final=false`; the terminal packet uses complete text with `status=SUCCESS` and `final=true`.
- `deliveryId` and `chatResponseAck` apply only to the terminal packet.
- Keep at most one Qwen observation in flight and one latest-wins pending frame per connection.
- Visual freshness wait is bounded to 4.0 seconds and never starts a foreground `video_understanding` tool call.
- Do not log or persist user text, answer chunks, frame paths, Base64/Hex media, or Provider raw responses.
- Do not modify or stage the pre-existing user change in `tests/test_phase0_service_boundary_contracts.py`.
- Follow repository policy: one final local commit after code, tests, and docs pass; no push or PR.

---

### Task 1: Stream Gateway chunks through the Media protocol

**Files:**
- Modify: `src/assistant_agent/services/gateway_turn_facade.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_gateway_turn_facade.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**
- Produces: `GatewayTurnFacade.run_turn(request, *, on_stream_chunk=None)` where the callback is `Callable[[str, Frame], Awaitable[None]]`.
- Produces: `_streaming_chat_response(prepared, *, delta, sequence) -> dict[str, Any]`.
- Preserves: `GatewayTurnResult.response_text` is the complete concatenated answer and `_prepared_chat_response()` creates the terminal packet.

- [ ] **Step 1: Add a failing facade callback test**

Add a test that runs a fake Gateway turn emitting `stream.chunk("你")`, `stream.chunk("好")`, and `run.end`; collect callback values and assert both deltas arrive before `run_turn()` returns while `result.response_text == "你好"`.

```python
seen: list[str] = []
result = await facade.run_turn(
    request,
    on_stream_chunk=lambda text, _frame: _append_async(seen, text),
)
assert seen == ["你", "好"]
assert result.response_text == "你好"
```

- [ ] **Step 2: Run the facade test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py -k stream_chunk_callback -q
```

Expected: FAIL because `run_turn()` does not accept `on_stream_chunk`.

- [ ] **Step 3: Implement the facade callback**

Add this alias and optional keyword argument, thread it into `_collect_turn()`, and await it immediately after extracting a non-empty `stream.chunk`:

```python
from collections.abc import Awaitable, Callable, Mapping

GatewayStreamChunkConsumer = Callable[[str, Frame], Awaitable[None]]

async def run_turn(
    self,
    request: GatewayTurnRequest,
    *,
    on_stream_chunk: GatewayStreamChunkConsumer | None = None,
) -> GatewayTurnResult:
    ...

if received.get("type") == "stream.chunk":
    chunk = _chunk_text(received)
    chunks.append(chunk)
    if chunk and on_stream_chunk is not None:
        await on_stream_chunk(chunk, dict(received))
    continue
```

- [ ] **Step 4: Verify facade GREEN**

Run the command from Step 2 and expect PASS.

- [ ] **Step 5: Add failing Agent-Service stream protocol tests**

Cover all four behaviors in `tests/test_agent_service_websocket.py`:

```python
assert [_body(item)["message"]["content"]["intentResult"]["description"] for item in packets] == ["你", "好", "你好"]
assert [_body(item)["message"]["content"]["intentResult"]["status"] for item in packets] == ["PROCESSING", "PROCESSING", "SUCCESS"]
assert [_body(item)["final"] for item in packets] == [False, False, True]
assert "deliveryId" not in _body(packets[0])
assert _body(packets[-1])["deliveryId"] == delivery.delivery_id
```

Also assert `stream=false` sends one terminal packet and a provider path with no response deltas sends one terminal packet even when `stream=true`.

- [ ] **Step 6: Run Agent-Service stream tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -k 'stream_true or stream_false or no_token_delta' -q
```

Expected: FAIL because Agent-Service still disables streaming and only sends a terminal response.

- [ ] **Step 7: Implement Media packet projection**

In `_run_chat_delivery()`, create a per-turn sequence counter and pass an async callback only when `prepared.body.get("stream") is True`. The callback must call `_send_response()` under the existing connection send lock:

```python
sequence = 0

async def send_delta(delta: str, _frame: dict[str, Any]) -> None:
    nonlocal sequence
    sequence += 1
    await _send_response(
        websocket,
        _streaming_chat_response(prepared, delta=delta, sequence=sequence),
        state=state,
    )
```

Set the Gateway request config to `response_streaming=prepared_stream_requested` instead of always false. Add terminal fields without changing the existing nested content:

```python
intent = {"description": delta, "status": "PROCESSING"}
body = {
    "message": {"chatIndex": prepared.chat_index, "content": {"intentResult": intent}},
    "display_only": False,
    "sequence": sequence,
    "final": False,
}
```

Terminal Media response uses `sequence=intermediate_count + 1` and `final=true`. Do not attach `deliveryId` to intermediate packets.

- [ ] **Step 8: Verify stream protocol GREEN and regressions**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py tests/test_agent_service_websocket.py tests/test_runtime_provider_streaming.py tests/test_realtime_agent_backend.py -q
```

Expected: all selected tests PASS.

---

### Task 2: Render live-camera context as natural shared vision

**Files:**
- Modify: `src/assistant_agent/agent/system_prompt_policy.py`
- Modify: `src/assistant_agent/services/context/renderer.py`
- Modify: `tests/test_system_prompt_policy.py`
- Modify: `tests/test_assistant_context_renderer.py`
- Modify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Produces: `render_request_context()` omits the upload-style video ID line only when request metadata proves trusted Agent-Service realtime entry.
- Produces: realtime phone prompt rules that distinguish a shared live camera from an uploaded video.

- [ ] **Step 1: Add failing prompt and renderer tests**

Build a trusted request with `transport=agent_service_websocket`, Gateway session `entry_profile=agent_service`, and a video ID. Assert:

```python
rendered = render_request_context(request)
assert "附带视频 ID" not in rendered
assert "agent-service-video" not in rendered
assert "当前通话的实时镜头" in rendered

instruction = render_system_instruction(SystemPromptProfile.REALTIME_PHONE)
assert "双方正在共享的当前镜头" in instruction
assert "你刚发送的视频" in instruction
assert "不得" in instruction
```

Add a normal upload request assertion that still contains `附带视频 ID`.

- [ ] **Step 2: Run the tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_system_prompt_policy.py tests/test_assistant_context_renderer.py tests/test_native_tool_call_handoff.py -k 'live_camera or realtime_video_wording' -q
```

Expected: FAIL on current upload-style rendering and absent phone rule.

- [ ] **Step 3: Implement trusted-entry rendering and spoken rules**

Add `_is_trusted_agent_service_request(request)` in the renderer using the same transport plus trusted session profile checks as runtime. Render:

```python
if request.video_ids:
    if _is_trusted_agent_service_request(request):
        lines.append("当前通话的实时镜头已连接；只有用户问题需要视觉事实时才使用。")
    else:
        lines.append(f"附带视频 ID：{request.video_ids}")
```

Add one phone rule:

```python
_PHONE_LIVE_CAMERA_RULES = (
    "Live camera: 实时视频上下文是双方正在共享的当前镜头，不是用户上传或刚发送的视频文件。需要视觉事实时自然地说‘我看到……’或‘看起来……’；不得说‘你刚发送的视频’，不得提到视频 ID、快照、后台观察、上下文注入或 Provider。画面仍在刷新或证据陈旧时要简短说明不确定性，不得把旧观察断言为当前事实。",
)
```

Insert it into `_render_realtime_phone()` before display rules.

- [ ] **Step 4: Verify prompt/context GREEN**

Run the command from Step 2 and then the full three files; expect PASS.

---

### Task 3: Add sequence-based realtime video freshness barrier

**Files:**
- Modify: `src/assistant_agent/schemas/context.py`
- Modify: `src/assistant_agent/services/realtime_video_memory.py`
- Modify: `src/assistant_agent/services/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_realtime_video_memory.py`
- Modify: `tests/test_realtime_video_observer.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**
- Produces: `RealtimeVideoContext.frame_capture_age_ms`, `snapshot_publish_age_ms`, `target_sequence`, and `sequence_gap`.
- Produces: `RealtimeVideoObserver.promote(frame) -> FrameProcessingResult` that bypasses adaptive selection but reuses the same queue and governance path.
- Produces: `RealtimeVideoObserver.wait_for_snapshot_sequence(sequence: int) -> None`.

- [ ] **Step 1: Add failing memory age tests**

Record a successful frame with `timestamp_ms=10_000` and diagnostics `published_at_ms=12_000`; project at `now_ms=15_000` and assert:

```python
assert context.snapshot_age_ms == 5_000
assert context.frame_capture_age_ms == 5_000
assert context.snapshot_publish_age_ms == 3_000
```

Add missing timestamp and future timestamp cases; both must return `frame_capture_age_ms is None` rather than clamp a bogus age.

- [ ] **Step 2: Run memory tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_memory.py -k 'capture_age or publish_age' -q
```

Expected: FAIL because the new fields do not exist and `snapshot_age_ms` uses publish time.

- [ ] **Step 3: Implement prompt-safe age projection**

Add optional non-negative fields to `RealtimeVideoContext`. In projection, use:

```python
capture_age = _past_age_ms(snapshot.last_success_timestamp_ms, now_ms)
publish_age = _past_age_ms(published_at_ms, now_ms)
snapshot_age = capture_age if capture_age is not None else publish_age
```

Where `_past_age_ms(value, now_ms)` returns `None` when `value is None` or `value > now_ms`.

- [ ] **Step 4: Verify memory GREEN**

Run the command from Step 2 and expect PASS.

- [ ] **Step 5: Add failing observer sequence tests**

Use a blocking fake executor and three frames. Assert `promote(frame3)` while frame1 is in flight leaves one in-flight plus frame3 pending, replaces frame2, and `wait_for_snapshot_sequence(3)` completes only after sequence 3 succeeds.

```python
waiter = asyncio.create_task(observer.wait_for_snapshot_sequence(3))
await asyncio.sleep(0)
assert not waiter.done()
release_frame_1.set()
await first_completed.wait()
assert not waiter.done()
release_frame_3.set()
await asyncio.wait_for(waiter, 1)
assert store.snapshot(video_id).last_success_sequence == 3
```

- [ ] **Step 6: Run observer tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_observer.py -k 'promote or snapshot_sequence' -q
```

Expected: FAIL because `promote()` and `wait_for_snapshot_sequence()` do not exist.

- [ ] **Step 7: Implement observer promotion and condition waiting**

Refactor selected-frame enqueueing into `_enqueue(frame)`. `submit()` retains adaptive selection; `promote()` calls `_enqueue()` directly. Maintain waiters under the event loop:

```python
async def wait_for_snapshot_sequence(self, sequence: int) -> None:
    while not self.closed:
        snapshot = self.memory_store.snapshot(self.video_id) if self.video_id else None
        if snapshot is not None and (snapshot.last_success_sequence or 0) >= sequence:
            return
        self._snapshot_updated.clear()
        await self._snapshot_updated.wait()
    raise RuntimeError("realtime video observer is closed")
```

Set `_snapshot_updated` after success/failure/pending transitions and on close. Promotion must still enqueue `_QueuedObservation`; it must not call `_execute_observation()` directly.

- [ ] **Step 8: Verify observer GREEN**

Run the command from Step 6 and the full observer test file; expect PASS.

- [ ] **Step 9: Add failing Agent-Service freshness tests**

Cover:

- latest decoded sequence 5, successful snapshot 3, no pending refresh: visual query promotes frame 5 and waits for sequence 5;
- existing in-flight sequence 5: visual query waits without a second execution;
- 4-second timeout: request metadata/context reports target 5, snapshot 3, gap 2, freshness false;
- greeting: no promotion and no wait.

Use a patched `VIDEO_FRESHNESS_WAIT_SECONDS` of `0.01` for timeout tests; do not sleep four real seconds.

- [ ] **Step 10: Run Agent-Service freshness tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -k 'freshness or target_sequence or promotes_latest' -q
```

Expected: FAIL because current code only waits for the first pending snapshot.

- [ ] **Step 11: Implement the freshness barrier**

Replace initial-only waiting with a helper that obtains the latest frame from runtime `video_context_store`, records its sequence, promotes it when the snapshot is behind and no equal/newer work is represented, and waits with:

```python
await asyncio.wait_for(
    observer.wait_for_snapshot_sequence(target_sequence),
    timeout=VIDEO_FRESHNESS_WAIT_SECONDS,
)
```

Set safe metadata keys `realtime_video_target_sequence`, `realtime_video_snapshot_sequence`, `realtime_video_sequence_gap`, `realtime_video_freshness_waited_ms`, and `realtime_video_freshness_satisfied`. Keep the narrow explicit visual reference gate.

- [ ] **Step 12: Verify freshness GREEN and cross-component regressions**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_agent_service_websocket.py tests/test_native_tool_call_handoff.py -q
```

Expected: all selected tests PASS and existing one-in-flight/latest-wins assertions remain green.

---

### Task 4: Safe diagnostics, authority docs, and complete verification

**Files:**
- Modify: `src/assistant_agent/services/agent_service_latency.py`
- Modify: `src/assistant_agent/services/context/observability.py`
- Modify: `tests/test_agent_service_latency.py`
- Modify: `tests/test_assistant_context_renderer.py`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Produces: safe stream and freshness diagnostics without content.
- Preserves: `agent_service_turn_latency_v1` terminal summary and existing trace redaction.

- [ ] **Step 1: Add failing safe-diagnostic tests**

Assert context trace and latency summaries expose only counts, booleans, sequence values, and ages:

```python
assert video.frame_capture_age_ms == 5_000
assert video.snapshot_publish_age_ms == 3_000
assert video.sequence_gap == 2
assert summary.stream_chunk_count == 2
assert summary.provider_token_stream_seen is True
assert "description" not in summary.model_dump_json()
assert "/tmp/frame" not in summary.model_dump_json()
```

- [ ] **Step 2: Run diagnostics tests and verify RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_latency.py tests/test_assistant_context_renderer.py -k 'stream_diagnostic or freshness_diagnostic' -q
```

Expected: FAIL because the fields are absent.

- [ ] **Step 3: Implement bounded diagnostics**

Extend Pydantic summary models with optional numeric/boolean fields from the design. Populate them from request metadata and safe context trace attributes. Never copy chunk text, Qwen summary, frame URI, or conversation text.

- [ ] **Step 4: Verify diagnostics GREEN**

Run the command from Step 2 and expect PASS.

- [ ] **Step 5: Update authority documentation**

Document exact Media packet examples:

```json
{"message":"chatResponse","body":"{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"你\",\"status\":\"PROCESSING\"}}},\"sequence\":1,\"final\":false,\"display_only\":false}"}
```

State that final packets contain the full answer, only final packets carry `deliveryId`, realtime camera wording differs from uploads, and freshness uses frame capture time plus a 4-second target-sequence barrier.

- [ ] **Step 6: Run focused verification**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_gateway_event_mapping.py tests/test_realtime_agent_backend.py tests/test_runtime_provider_streaming.py tests/test_agent_service_websocket.py tests/test_agent_service_latency.py tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_assistant_context_renderer.py tests/test_native_tool_call_handoff.py tests/test_system_prompt_policy.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 7: Run environment, fast suite, and diff checks**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check
```

Expected: environment check reports `ok=true`, fast suite PASS, and diff check has no output.

- [ ] **Step 8: Run explicit real-provider smoke**

Only with the user's existing local credentials and process-level `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke`, run a Media-protocol greeting and current-camera question. Confirm safe evidence: DeepSeek first delta precedes final `chatResponse`, final text matches concatenated deltas, greeting does not mention video, Qwen remains single-flight, and the visual turn reports a satisfied target sequence or an explicit stale timeout. Do not persist raw media or Provider responses.

- [ ] **Step 9: Review and create one task commit**

Review only task files, exclude `tests/test_phase0_service_boundary_contracts.py`, then commit:

```bash
git status --short
git diff --stat
git diff --check
git add docs/superpowers/specs/2026-07-14-agent-service-streaming-video-freshness-design.md docs/superpowers/plans/2026-07-14-agent-service-streaming-video-freshness.md src/assistant_agent/services/gateway_turn_facade.py src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/agent/system_prompt_policy.py src/assistant_agent/services/context/renderer.py src/assistant_agent/schemas/context.py src/assistant_agent/services/realtime_video_memory.py src/assistant_agent/services/realtime_video_observer.py src/assistant_agent/services/agent_service_latency.py src/assistant_agent/services/context/observability.py tests/test_gateway_turn_facade.py tests/test_agent_service_websocket.py tests/test_system_prompt_policy.py tests/test_assistant_context_renderer.py tests/test_native_tool_call_handoff.py tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_agent_service_latency.py docs/media-agent-service-websocket.md docs/gateway-architecture.md docs/CONTEXT_ENGINEERING_STATUS.md docs/observability-harness.md
git commit -m "fix: stream realtime replies with fresh video context"
```

Expected: commit succeeds and the unrelated pre-existing test modification remains unstaged.
