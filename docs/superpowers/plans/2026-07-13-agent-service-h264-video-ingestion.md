# Agent Service H.264 Video Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decode self-contained H.264 I-frame messages received by `/agent-service/v1`, expose their recent JPEG frames to the existing runtime, and let the LLM autonomously call the governed `video_understanding` tool for a later chat turn.

**Architecture:** A focused entry-layer ingestion service validates Hex H.264, invokes the existing FFmpeg binary once per independent I-frame, stores a bounded JPEG window, and appends matching `VideoFrame` records to the global runtime's `VideoContextStore`. The WebSocket handler records a stable session video id and adds it to the next Gateway chat request; all understanding remains inside the existing validator/executor/registry/provider chain.

**Tech Stack:** Python 3.12, FastAPI WebSocket, Pydantic models, system FFmpeg 6.x, pytest, existing `AgentGraphRuntime` and `VideoContextStore`.

## Global Constraints

- Do not install PyAV, OpenCV, NumPy, or any new dependency.
- Do not create a second MLLM client or call a provider from the WebSocket handler.
- Do not persist raw H.264 payloads or log complete Hex/JPEG/provider responses.
- Keep decoded artifacts bounded to the most recent three frames per session video id.
- Keep provider defaults mock/local/offline; real Ark calls remain explicit `provider_smoke` validation only.
- Preserve all existing user changes and the then-untracked media-side H.264 guide (that guide was subsequently retired by the repository documentation-sync pass).

---

## File Structure

- Create `src/assistant_agent/services/h264_video_ingestion.py`: validation, FFmpeg decode, artifact ownership, and `VideoFrame` registration.
- Create `tests/test_h264_video_ingestion.py`: focused unit tests with an injected decoder and controlled temporary directory.
- Modify `src/assistant_agent/services/video_context.py`: expose removal/clear operations needed for consistent eviction and disconnect cleanup.
- Modify `tests/test_video_context.py`: prove removal behavior without changing append/read semantics.
- Modify `src/assistant_agent/api/agent_service_websocket.py`: resolve the shared runtime store, ingest frames, bind `video_ids`, and clean up connection artifacts.
- Modify `tests/test_agent_service_websocket.py`: protocol failures, ACK meaning, Gateway propagation, and connection cleanup.
- Modify `src/assistant_agent/gateway/capabilities.py`: advertise video-reference and raw-media support for this entry.
- Modify `tests/test_native_tool_call_handoff.py`: scripted real-chat autonomous video tool call through the governed chain.
- Modify `docs/media-agent-service-websocket.md` and `docs/gateway-architecture.md`: authoritative behavior and operating constraints.

### Task 1: Removable Video Context Window

**Files:**
- Modify: `src/assistant_agent/services/video_context.py`
- Modify: `tests/test_video_context.py`

**Interfaces:**
- Produces: `VideoContextStore.remove_video(video_id: str) -> list[VideoFrame]`
- Produces: `InMemoryVideoContextStore.remove_video(video_id: str) -> list[VideoFrame]`
- Existing `append_frame()` and `get_recent_frames()` signatures remain unchanged.

- [ ] **Step 1: Write the failing removal test**

```python
def test_video_context_store_remove_video_returns_and_clears_frames() -> None:
    store = InMemoryVideoContextStore(window_size=3)
    frame = VideoFrame(video_id="video1", frame_id="frame_1", uri="frame_1.jpg", sequence=1)
    store.append_frame(frame)

    removed = store.remove_video("video1")

    assert removed == [frame]
    assert store.get_recent_frames("video1") == []
    assert store.remove_video("video1") == []
```

- [ ] **Step 2: Run the test and confirm the expected RED state**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_video_context.py::test_video_context_store_remove_video_returns_and_clears_frames -q`

Expected: FAIL with `AttributeError: 'InMemoryVideoContextStore' object has no attribute 'remove_video'`.

- [ ] **Step 3: Add the minimal protocol and implementation method**

```python
class VideoContextStore(Protocol):
    # existing methods
    def remove_video(self, video_id: str) -> list[VideoFrame]:
        """Remove and return all retained frames for one video id."""

class InMemoryVideoContextStore:
    # existing methods
    def remove_video(self, video_id: str) -> list[VideoFrame]:
        with self._lock:
            return self._frames.pop(video_id, [])
```

- [ ] **Step 4: Run focused video context tests**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_video_context.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the context-store contract**

```bash
git add src/assistant_agent/services/video_context.py tests/test_video_context.py
git commit -m "Add removable video context windows"
```

### Task 2: H.264 Frame Ingestion Service

**Files:**
- Create: `src/assistant_agent/services/h264_video_ingestion.py`
- Create: `tests/test_h264_video_ingestion.py`

**Interfaces:**
- Consumes: `VideoContextStore.append_frame()` and `remove_video()` from Task 1.
- Produces: `H264VideoIngestionService(store, root, decoder, window_size=3, max_frame_bytes=8_388_608)`.
- Produces: `ingest(session_id: str, frame_index: str, video_hex: str, video_config: dict[str, Any], timestamp: str | None) -> VideoFrame`.
- Produces: `cleanup(video_id: str) -> None` and `video_id_for_session(session_id: str) -> str`.
- Produces: `H264VideoIngestionError`, containing a prompt-safe message only.

- [ ] **Step 1: Write failing tests for valid ingestion and stable opaque ids**

```python
def test_ingest_decodes_hex_registers_jpeg_and_uses_opaque_video_id(tmp_path) -> None:
    store = InMemoryVideoContextStore(window_size=3)
    calls = []

    def decoder(data: bytes, destination: Path, timeout_s: float) -> None:
        calls.append((data, destination, timeout_s))
        destination.write_bytes(b"\xff\xd8jpeg\xff\xd9")

    service = H264VideoIngestionService(store=store, root=tmp_path, decoder=decoder)
    frame = service.ingest(
        session_id="../../../private-session",
        frame_index="7",
        video_hex="0000000167aa0000000168bb0000000165cc",
        video_config={"codec": "H264", "resolution": "1280x720", "frameRate": 25},
        timestamp="2026-07-13T08:30:00Z",
    )

    assert calls[0][0].startswith(b"\x00\x00\x00\x01")
    assert frame.video_id == service.video_id_for_session("../../../private-session")
    assert "private-session" not in frame.uri
    assert Path(frame.uri).read_bytes().startswith(b"\xff\xd8")
    assert store.get_recent_frames(frame.video_id) == [frame]
```

- [ ] **Step 2: Run the valid-ingestion test and confirm RED**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_h264_video_ingestion.py::test_ingest_decodes_hex_registers_jpeg_and_uses_opaque_video_id -q`

Expected: collection FAIL because `assistant_agent.services.h264_video_ingestion` does not exist.

- [ ] **Step 3: Implement the service shell and injected decoder boundary**

Implement `H264VideoIngestionService`, SHA-256-derived `agent-service-video-<digest>` ids, strict Hex decoding, Annex-B start-code checks, safe filenames from a numeric internal sequence, JPEG existence validation, and `VideoFrame` registration. Use `datetime.fromisoformat()` only to derive optional `timestamp_ms`; invalid timestamps remain metadata strings and do not fail frame ingestion.

- [ ] **Step 4: Run the valid-ingestion test and confirm GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Add parameterized failing validation tests**

```python
@pytest.mark.parametrize(
    ("video_hex", "config", "message"),
    [
        ("xyz", {"codec": "H264"}, "valid hexadecimal"),
        ("001", {"codec": "H264"}, "even number"),
        ("00112233", {"codec": "H264"}, "Annex-B"),
        ("0000000165aa", {"codec": "VP8"}, "H264"),
    ],
)
def test_ingest_rejects_invalid_transport(video_hex, config, message, tmp_path) -> None:
    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(), root=tmp_path, decoder=lambda *_: None
    )
    with pytest.raises(H264VideoIngestionError, match=message):
        service.ingest("s1", "1", video_hex, config, None)
```

Add separate tests for `max_frame_bytes`, decoder timeout/error mapping, empty decoder output, three-frame eviction deleting the oldest JPEG, and `cleanup()` deleting retained JPEGs plus store entries.

- [ ] **Step 6: Run validation tests and confirm the expected failures**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_h264_video_ingestion.py -q`

Expected: new tests FAIL on missing validation, cleanup, or eviction behavior.

- [ ] **Step 7: Implement limits, eviction, cleanup, and production FFmpeg decoder**

The default decoder must execute without a shell:

```python
subprocess.run(
    [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
     "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", str(destination)],
    input=h264_bytes,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    timeout=timeout_s,
    check=False,
)
```

Map missing FFmpeg, timeout, non-zero exit, and missing JPEG to `H264VideoIngestionError` without including raw input or unbounded stderr. Track artifacts under the service lock and delete the evicted URI after every append.

- [ ] **Step 8: Run all ingestion and context tests**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_h264_video_ingestion.py tests/test_video_context.py -q`

Expected: all tests PASS.

- [ ] **Step 9: Commit the ingestion service**

```bash
git add src/assistant_agent/services/h264_video_ingestion.py tests/test_h264_video_ingestion.py
git commit -m "Decode agent service H264 frames"
```

### Task 3: Agent-Service WebSocket And Gateway Wiring

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/gateway/capabilities.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**
- Consumes: Task 2 `H264VideoIngestionService.ingest()`, `cleanup()`, and `video_id_for_session()`.
- Produces: `AgentServiceConnectionState.video_ids: list[str]` and `video_ingestion` connection service.
- Produces: `GatewayTurnRequest(video_ids=list(state.video_ids))` for chat turns.

- [ ] **Step 1: Write the failing WebSocket propagation test**

Use a fake ingestion service whose `ingest()` appends a known `VideoFrame`, send `assistantControl`, a valid `video` envelope, then `chat`, and assert:

```python
assert _body(video_response) == {"code": 0, "message": "video received"}
assert runtime.requests[0].video_ids == ["agent-service-video-test"]
assert runtime.requests[0].metadata["runtime"]["entry_capabilities"]["supports_video_refs"] is True
assert runtime.requests[0].metadata["runtime"]["entry_capabilities"]["supports_raw_media"] is True
```

Expose a small `_create_video_ingestion_service()` factory in the module so the test can monkeypatch it without replacing production handler behavior.

- [ ] **Step 2: Run the propagation test and confirm RED**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py::test_agent_service_video_context_reaches_following_chat -q`

Expected: FAIL because video ingestion and `video_ids` propagation are absent.

- [ ] **Step 3: Wire connection state, handler ingestion, and Gateway request**

Resolve `routes_agent.get_assistant_runtime_app().runtime.video_context_store` once per connection, construct the ingestion service, extract each content item's Hex/time, and ingest it with the message `videoIndex` and `videoConfig`. Record the stable id once in state. Pass `video_ids=list(state.video_ids)` into `GatewayTurnRequest`.

Set capabilities exactly as follows:

```python
AGENT_SERVICE_ENTRY_CAPABILITIES = EntryAdapterCapabilities(
    supports_text_streaming=False,
    supports_interrupt=False,
    supports_realtime_task_state=True,
    supports_video_refs=True,
    supports_raw_media=True,
)
```

- [ ] **Step 4: Run the propagation test and confirm GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Add failing protocol and cleanup tests**

Assert an invalid Hex frame returns `videoResponse` with `code=FAIL`, does not add `video_ids`, and allows a later chat. Assert leaving the WebSocket context invokes `cleanup(video_id)` for every connection-owned id. Update the prior ACK-only media test to inject a successful fake ingestion service rather than treating `00ff` as valid H.264.

- [ ] **Step 6: Run new tests and confirm RED where cleanup/error mapping is missing**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -q`

Expected: targeted new tests FAIL; existing tests remain otherwise compatible.

- [ ] **Step 7: Add prompt-safe error mapping and disconnect cleanup**

Catch `H264VideoIngestionError` through the existing handler failure envelope. In the route `finally`, call ingestion cleanup before closing the Gateway manager. Do not log frame contents; keep only message byte count, session id, video id, and accepted sequence metadata.

- [ ] **Step 8: Run WebSocket, Gateway, and architecture tests**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py tests/test_phase0_entrypoint_contracts.py tests/test_architecture_boundaries.py -q`

Expected: all tests PASS.

- [ ] **Step 9: Commit the transport wiring**

```bash
git add src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/gateway/capabilities.py tests/test_agent_service_websocket.py
git commit -m "Route agent service video into Gateway"
```

### Task 4: Autonomous Governed Video Tool Call

**Files:**
- Modify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Consumes: `UserRequest.video_ids`, runtime shared `VideoContextStore`, and existing provider-native tool-call loop.
- Proves: a non-mock scripted chat adapter selects `video_understanding` and the executor provides recent JPEG `frame_refs` to the video adapter.

- [ ] **Step 1: Write the scripted assistant-loop integration test**

Build an `InMemoryVideoContextStore` containing three temporary JPEG paths, a capturing video adapter, and a scripted real-chat adapter that first returns:

```python
NativeToolCall(
    id="call_video_1",
    name="video_understanding",
    arguments={"video_ids": ["agent-service-video-test"], "user_query": "识别眼前物体"},
)
```

Then return a final grounded response. Assert the request reaches validator/executor, the tool record succeeds, captured `frame_refs` equal the three stored paths, and the final answer is returned.

- [ ] **Step 2: Run the integration test and confirm behavior**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py::test_native_video_tool_call_uses_agent_service_frame_context -q`

Expected: PASS if Tasks 1-3 preserve the existing governed chain; otherwise FAIL at the broken contract and fix only that contract.

- [ ] **Step 3: Run adjacent native tool and video tests**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py tests/test_video_understanding_tool.py tests/test_video_context.py tests/test_action_validator.py -q`

Expected: all tests PASS.

- [ ] **Step 4: Commit the end-to-end regression test**

```bash
git add tests/test_native_tool_call_handoff.py
git commit -m "Test autonomous video understanding handoff"
```

### Task 5: Documentation And End-To-End Verification

**Files:**
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/gateway-architecture.md`
- Optionally modify: `scripts/realtime_media_client.py` only if the existing script already supports `/agent-service/v1`; otherwise use an inline local smoke client without committing it.

**Interfaces:**
- Documents the same H.264/ACK/tool-selection contract implemented in Tasks 1-4.

- [ ] **Step 1: Update authoritative documentation**

Replace ACK-only statements with the exact flow:

```text
video Hex -> strict H264 validation -> FFmpeg single-I-frame decode -> bounded JPEG context
-> later chat carries video_ids -> LLM autonomously chooses video_understanding
-> validator -> executor -> registry -> configured video provider
```

Document self-contained SPS/PPS/I-frame input, the three-frame window, recoverable failure responses, artifact cleanup, `provider_smoke` requirement for real Ark calls, and no raw payloads in prompts/traces.

- [ ] **Step 2: Run documentation and whitespace checks**

Run: `git diff --check -- docs/media-agent-service-websocket.md docs/gateway-architecture.md src/assistant_agent tests`

Expected: exit code 0 with no output.

- [ ] **Step 3: Run the complete focused suite**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_h264_video_ingestion.py tests/test_video_context.py tests/test_agent_service_websocket.py tests/test_native_tool_call_handoff.py tests/test_video_understanding_tool.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py tests/test_phase0_entrypoint_contracts.py tests/test_architecture_boundaries.py -q`

Expected: all tests PASS.

- [ ] **Step 4: Run environment and fast-suite regression checks**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py`

Expected: environment report completes without enabling an unrequested provider.

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q`

Expected: all fast tests PASS.

- [ ] **Step 5: Restart the local provider-smoke server and perform a real opt-in smoke**

Restart the existing server on port `8089` only after preserving its current explicit `provider_smoke` configuration. Send one valid self-contained H.264 sample frame followed by `识别眼前物体`. Verify `videoResponse.code=0`, `chatResponse.status=SUCCESS`, and query the returned run/trace records to confirm `video_understanding`, provider `ark`, a successful observation, and recent JPEG frame references. Do not commit the sample frame, decoded JPEG, trace payload, or provider response.

If no valid media sample is available, report the real-provider smoke as not run; do not fabricate success from unit fixtures.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/media-agent-service-websocket.md docs/gateway-architecture.md
git commit -m "Document agent service video ingestion"
```

- [ ] **Step 7: Review final diff and repository status**

Run: `git status --short && git log --oneline -6 && git diff HEAD~5..HEAD --stat`

Expected at the time: only the user-owned media-side H.264 guide remains untracked; all implementation commits are present and verification results are recorded for the final report. The guide was subsequently retired by the repository documentation-sync pass.
