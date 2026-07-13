# Continuous Keyframe Video Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect `/agent-service/v1` H.264 ingestion to bounded adaptive keyframe observation so later Agent visual questions use rolling semantic video memory without a query-time visual MLLM call, with recent-frame Provider fallback when memory is unavailable or failed.

**Architecture:** FFmpeg emits each JPEG plus a small grayscale fingerprint. A per-WebSocket observer reuses the existing adaptive selector, retains selected artifacts, and serially executes the existing `video_understanding` tool through `ActionValidator` and `ToolExecutor`. A runtime-owned semantic store lets ordinary `video_understanding` calls return a healthy snapshot immediately or fall back to the current recent-frame adapter path.

**Tech Stack:** Python 3.12, FastAPI WebSocket, asyncio, FFmpeg, Pydantic, existing LangGraph runtime/tool governance, pytest.

## Global Constraints

- Do not add dependencies; `/usr/bin/ffmpeg` remains the only media decoder requirement.
- Default `local_demo` and `offline_eval` profiles make no network Provider calls.
- Real continuous MLLM calls require explicit `provider_smoke` or `pilot` configuration.
- All external visual calls pass through `ActionValidator`, `ToolExecutor`, and `ToolRegistry`.
- Keep the vendor WebSocket envelope and existing `videoResponse`, `chatResponse`, progress, and ACK contracts compatible.
- Keep raw H.264, grayscale fingerprints, credentials, and Provider raw responses out of prompts, traces, and semantic memory.
- Retain 3 raw frames, at most 8 semantic keyframes, and at most one pending plus one in-flight observation.
- Use TDD for every behavior change and commit each task separately.

---

## File Structure

- Modify `src/assistant_agent/services/video_context.py`: carry an in-memory fixed grayscale fingerprint with a decoded frame.
- Modify `src/assistant_agent/services/h264_video_ingestion.py`: produce JPEG and fingerprint in one FFmpeg process.
- Create `src/assistant_agent/video_ai/keyframe/collector.py`: reusable local-only adaptive selection, separated from MLLM observation.
- Modify `src/assistant_agent/video_ai/app.py`: preserve the existing demo API by composing the new collector with its existing vision client.
- Create `src/assistant_agent/services/realtime_video_memory.py`: thread-safe prompt-safe per-video semantic snapshots.
- Modify `src/assistant_agent/tools/video_tool.py`: resolve healthy rolling memory before the existing Provider fallback, except in internal observation mode.
- Modify `src/assistant_agent/tools/registry.py` and `src/assistant_agent/agent/runtime.py`: inject one runtime-owned semantic store into the existing tool.
- Create `src/assistant_agent/services/realtime_video_observer.py`: retain selected frames, coalesce background work, and invoke governed analysis.
- Modify `src/assistant_agent/api/agent_service_websocket.py`: schedule observer work after ingestion and close it before raw-frame cleanup.
- Modify `docs/media-agent-service-websocket.md`, `docs/gateway-architecture.md`, and `docs/observability-harness.md`: document acceptance semantics, memory-first resolution, fallback, and trace fields.
- Modify targeted tests under `tests/` for each contract.

---

### Task 1: FFmpeg Fingerprint And Reusable Local Collector

**Files:**
- Modify: `src/assistant_agent/services/video_context.py`
- Modify: `src/assistant_agent/services/h264_video_ingestion.py`
- Create: `src/assistant_agent/video_ai/keyframe/collector.py`
- Modify: `src/assistant_agent/video_ai/app.py`
- Modify: `src/assistant_agent/video_ai/detection/frame_difference.py`
- Test: `tests/test_h264_video_ingestion.py`
- Test: `tests/test_realtime_video_ai.py`

**Interfaces:**
- Produces: `VideoFrame.fingerprint: tuple[int, ...] | None`, `fingerprint_width`, and `fingerprint_height`.
- Produces: `DecodedFrameData(fingerprint, width, height)` from the H.264 decoder seam.
- Produces: `AdaptiveKeyframeCollector.collect(frame) -> KeyframeCollectionResult` with a selected frame or `None` and a local `FrameProcessingResult`.
- Preserves: `RealtimeVideoUnderstandingApp.process_frame()` behavior and existing tests.

- [ ] **Step 1: Write failing decoder and URI-safety tests**

Add tests that require a fake decoder result to reach the context frame and prove different URI strings do not create visual change without pixels:

```python
from assistant_agent.services.h264_video_ingestion import DecodedFrameData
from assistant_agent.video_ai.detection.frame_difference import FrameDifferenceDetector
from assistant_agent.video_ai.types import VideoFrame as AIVideoFrame


def test_ingest_registers_bounded_grayscale_fingerprint(tmp_path: Path) -> None:
    def decoder(_data: bytes, destination: Path, _timeout_s: float) -> DecodedFrameData:
        destination.write_bytes(b"\xff\xd8jpeg\xff\xd9")
        return DecodedFrameData(fingerprint=(0, 64, 128, 255), width=2, height=2)

    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(), root=tmp_path, decoder=decoder
    )
    frame = service.ingest("s1", "1", VALID_H264_HEX, {"codec": "H264"}, None)

    assert frame.fingerprint == (0, 64, 128, 255)
    assert (frame.fingerprint_width, frame.fingerprint_height) == (2, 2)


def test_uri_text_is_not_used_as_frame_pixels() -> None:
    detector = FrameDifferenceDetector(fingerprint_size=(2, 2))
    left = AIVideoFrame(frame_id="1", timestamp_seconds=0.0, uri="/a.jpg")
    right = AIVideoFrame(frame_id="2", timestamp_seconds=1.0, uri="/very/different/name.jpg")

    assert detector.compare(right, left).pixel_change_score == 0.0
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_h264_video_ingestion.py::test_ingest_registers_bounded_grayscale_fingerprint \
  tests/test_realtime_video_ai.py::test_uri_text_is_not_used_as_frame_pixels -q
```

Expected: FAIL because `DecodedFrameData` and fingerprint fields do not exist and URI currently becomes byte content.

- [ ] **Step 3: Implement one-pass JPEG plus grayscale output**

Add the immutable decoder result and frame fields:

```python
@dataclass(frozen=True)
class DecodedFrameData:
    fingerprint: tuple[int, ...] = ()
    width: int = 0
    height: int = 0


FrameDecoder = Callable[[bytes, Path, float], DecodedFrameData | None]
```

Store only the bounded fingerprint on `services.video_context.VideoFrame`:

```python
fingerprint: tuple[int, ...] | None = None
fingerprint_width: int | None = None
fingerprint_height: int | None = None
```

Change `_decode_h264_with_ffmpeg()` to split the decoded frame, write one JPEG, and return a `32x18` gray rawvideo buffer from stdout:

```python
filter_graph = "[0:v]split=2[full][thumb];[thumb]scale=32:18,format=gray[gray]"
result = subprocess.run(
    [
        ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-f", "h264",
        "-i", "pipe:0", "-filter_complex", filter_graph,
        "-map", "[full]", "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg",
        "-y", str(destination),
        "-map", "[gray]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ],
    input=h264_bytes,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=timeout_s,
    check=False,
)
if result.returncode != 0:
    raise H264VideoIngestionError("FFmpeg could not decode the H264 frame")
fingerprint = tuple(result.stdout[: 32 * 18])
return DecodedFrameData(fingerprint=fingerprint, width=32, height=18)
```

Treat a legacy test decoder returning `None` as no fingerprint. Remove the `frame.uri.encode()` fallback from `_extract_grayscale()` so only real pixels or an explicit `pixel_signature` participate.

- [ ] **Step 4: Write a failing collector compatibility test**

Add a test proving selection can run without calling a vision client and that the existing app still calls its client only after selection:

```python
def test_adaptive_collector_selects_without_mllm_call() -> None:
    collector = AdaptiveKeyframeCollector(
        semantic_detector=SemanticChangeDetector(MetadataEmbeddingModel()),
        keyframe_config=KeyframeSelectorConfig(min_interval_seconds=0.5),
    )

    first = collector.collect(_frame("first", 0.0, 10, embedding=[1.0, 0.0]))
    still = collector.collect(_frame("still", 0.2, 10, embedding=[1.0, 0.0]))

    assert first.selected_frame is not None
    assert first.processing.keyframe_selected is True
    assert first.processing.qwen_called is False
    assert still.selected_frame is None
```

- [ ] **Step 5: Run the collector test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_realtime_video_ai.py::test_adaptive_collector_selects_without_mllm_call -q
```

Expected: FAIL because `AdaptiveKeyframeCollector` does not exist.

- [ ] **Step 6: Extract and compose the collector**

Create:

```python
@dataclass(frozen=True)
class KeyframeCollectionResult:
    processing: FrameProcessingResult
    selected_frame: VideoFrame | None


class AdaptiveKeyframeCollector:
    def collect(self, frame: VideoFrame) -> KeyframeCollectionResult:
        started_at = time.perf_counter()
        metrics, errors = self._change_metrics(frame)
        force = self.selector.force_due(frame.timestamp_seconds, self._last_keyframe_at)
        sampling = self.sampler.should_sample(
            timestamp_seconds=frame.timestamp_seconds,
            change_score=0.0 if self._last_keyframe is None else metrics.change_score,
            force=force,
        )
        selected_frame = None
        reason = sampling.reason
        if sampling.sampled:
            decision = self.selector.select(
                frame, metrics, last_keyframe_at=self._last_keyframe_at
            )
            reason = decision.reason
            if decision.selected:
                selected_frame = frame
                self.semantic_detector.commit_current_embedding_as_keyframe(frame)
                self._last_keyframe = frame
                self._last_keyframe_at = frame.timestamp_seconds
        processing = FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=frame.timestamp_seconds,
            sampled=sampling.sampled,
            sampling_rate=sampling.sampling_rate,
            metrics=metrics,
            keyframe_selected=selected_frame is not None,
            qwen_called=False,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            decision_reason=reason,
            errors=errors,
        )
        self.log_records.append(_log_record(processing))
        return KeyframeCollectionResult(processing=processing, selected_frame=selected_frame)
```

Move the local decision state (`_last_keyframe`, `_last_keyframe_at`) into the collector. Refactor `RealtimeVideoUnderstandingApp.process_frame()` to call `collector.collect()`, then preserve its current storage, `understand_keyframe()`, memory update, and returned `qwen_called=True` behavior when a frame was selected.

- [ ] **Step 7: Run focused and existing video-AI tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_h264_video_ingestion.py tests/test_realtime_video_ai.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/assistant_agent/services/video_context.py \
  src/assistant_agent/services/h264_video_ingestion.py \
  src/assistant_agent/video_ai/keyframe/collector.py \
  src/assistant_agent/video_ai/app.py \
  src/assistant_agent/video_ai/detection/frame_difference.py \
  tests/test_h264_video_ingestion.py tests/test_realtime_video_ai.py
git commit -m "Add local adaptive keyframe collection"
```

---

### Task 2: Runtime Rolling Video Memory And Memory-First Tool Resolution

**Files:**
- Create: `src/assistant_agent/services/realtime_video_memory.py`
- Modify: `src/assistant_agent/schemas/perception.py`
- Modify: `src/assistant_agent/tools/video_tool.py`
- Modify: `src/assistant_agent/tools/registry.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Create: `tests/test_realtime_video_memory.py`
- Modify: `tests/test_video_context.py`

**Interfaces:**
- Produces: `RealtimeVideoMemoryStore.record_success()`, `record_failure()`, `mark_pending()`, `snapshot()`, and `remove_video()`.
- Produces: immutable `RealtimeVideoSnapshot` and `SemanticKeyframeRecord`.
- Changes: `VideoUnderstandingTool` accepts `memory_store` and uses `ToolContext.metadata["realtime_video_observation"]` to bypass memory only for governed background analysis.
- Changes: `AgentGraphRuntime.realtime_video_memory_store` is shared with its registry.

- [ ] **Step 1: Write failing rolling-memory isolation and health tests**

Create tests with two video ids:

```python
def test_video_memory_is_isolated_and_latest_failure_is_not_healthy() -> None:
    store = RealtimeVideoMemoryStore(max_keyframes=2)
    frame = SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/1.jpg", sequence=1, timestamp_ms=1000
    )
    store.record_success("video-a", frame, _result(summary="cup", objects=["cup"]))

    assert store.snapshot("video-a").healthy is True
    assert store.snapshot("video-b") is None

    store.record_failure("video-a", frame, {"code": "provider_timeout", "message": "timed out"})
    snapshot = store.snapshot("video-a")
    assert snapshot is not None
    assert snapshot.current_state == "cup"
    assert snapshot.healthy is False
    assert snapshot.last_error["code"] == "provider_timeout"
```

- [ ] **Step 2: Run memory tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_memory.py -q
```

Expected: FAIL because the store and models do not exist.

- [ ] **Step 3: Implement the bounded thread-safe semantic store**

Use frozen Pydantic models for snapshots and a lock around mutable internal state:

```python
class SemanticKeyframeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    frame_id: str
    uri: str
    sequence: int
    timestamp_ms: int | None = None


class RealtimeVideoSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    video_id: str
    current_state: str = ""
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    scene: str | None = None
    keyframes: list[SemanticKeyframeRecord] = Field(default_factory=list)
    last_success_sequence: int | None = None
    last_success_timestamp_ms: int | None = None
    last_observation_status: Literal["pending", "succeeded", "failed"] | None = None
    last_error: dict[str, Any] | None = None
    pending_count: int = 0
    in_flight: bool = False

    @property
    def healthy(self) -> bool:
        return self.last_success_sequence is not None and self.last_observation_status == "succeeded"
```

Merge successful structured fields, append one keyframe, trim to `max_keyframes`, preserve the successful state on failure, and return evicted keyframe records so the observer can delete artifacts.

- [ ] **Step 4: Write failing memory-first and fallback tests**

Use a counting adapter:

```python
def _result(*, summary: str, objects: list[str]) -> VideoUnderstandingResult:
    return VideoUnderstandingResult(
        summary=summary,
        objects=objects,
        provider="test-video",
        model="test-model",
        output_ref="provider://video/test/result",
    )


def _keyframe() -> SemanticKeyframeRecord:
    return SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/frame-1.jpg", sequence=1, timestamp_ms=1000
    )


class CountingVideoAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        self.calls += 1
        return _result(summary="fallback", objects=["fallback-object"])


def _context(video_id: str) -> InMemoryVideoContextStore:
    store = InMemoryVideoContextStore(window_size=3)
    store.append_frame(
        VideoFrame(video_id=video_id, frame_id="raw-1", uri="/tmp/raw-1.jpg", sequence=1)
    )
    return store


def _failed_after_success_memory() -> RealtimeVideoMemoryStore:
    store = RealtimeVideoMemoryStore()
    frame = _keyframe()
    store.record_success("video-a", frame, _result(summary="old", objects=["old-object"]))
    store.record_failure(
        "video-a", frame, {"code": "provider_timeout", "message": "timed out"}
    )
    return store


def test_video_tool_uses_healthy_memory_without_provider_call() -> None:
    adapter = CountingVideoAdapter()
    memory = RealtimeVideoMemoryStore()
    memory.record_success("video-a", _keyframe(), _result(summary="桌上有杯子", objects=["杯子"]))
    tool = VideoUnderstandingTool(adapter=adapter, context_store=InMemoryVideoContextStore(), memory_store=memory)

    result = tool.run({"video_ref": "video-a", "user_query": "眼前有什么？"})

    assert result.success is True
    assert result.data["source"] == "rolling_video_memory"
    assert result.data["objects"] == ["杯子"]
    assert adapter.calls == 0


def test_video_tool_falls_back_after_latest_observation_failure() -> None:
    adapter = CountingVideoAdapter()
    memory = _failed_after_success_memory()
    tool = VideoUnderstandingTool(adapter=adapter, context_store=_context("video-a"), memory_store=memory)

    result = tool.run({"video_ref": "video-a"})

    assert result.data["source"] == "recent_frame_fallback"
    assert adapter.calls == 1


def test_observation_context_forces_provider_even_with_healthy_memory() -> None:
    result = tool.run(
        {"video_ref": "video-a", "frame_refs": ["/tmp/keyframe.jpg"]},
        ToolContext(metadata={"realtime_video_observation": True}),
    )
    assert adapter.calls == 1
    assert result.data["source"] == "background_keyframe_observation"
```

- [ ] **Step 5: Run tool tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_video_context.py -k "healthy_memory or observation_failure or observation_context" -q
```

Expected: FAIL because `VideoUnderstandingTool` has no semantic store resolution.

- [ ] **Step 6: Implement tool resolution and runtime injection**

In `VideoUnderstandingTool._run()`:

```python
video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
observation_mode = context.metadata.get("realtime_video_observation") is True
if video_ref and not observation_mode and self.memory_store is not None:
    snapshot = self.memory_store.snapshot(video_ref)
    if snapshot is not None and snapshot.healthy:
        return self._memory_result(snapshot)

input = self._with_context_frames(input)
result = self.adapter.understand_video(input)
source = "background_keyframe_observation" if observation_mode else "recent_frame_fallback"
payload = {**result.model_dump(mode="json"), "source": source}
```

The memory result must keep the existing capability contract, set an opaque
`memory://realtime-video/<video-id>` output ref, and include `source`, snapshot
sequence, observed timestamp, and keyframe count. Add `people` to
`VideoUnderstandingResult` as an additive structured field.

Create one store in `AgentGraphRuntime.__init__()` and pass it through
`create_default_registry(config, video_context_store=frames,
realtime_video_memory_store=store)` into the
existing `VideoUnderstandingTool`.

- [ ] **Step 7: Run memory, tool, registry, and runtime tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_realtime_video_memory.py tests/test_video_context.py \
  tests/test_video_understanding_tool.py tests/unit/test_tool_registry.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/assistant_agent/services/realtime_video_memory.py \
  src/assistant_agent/schemas/perception.py src/assistant_agent/tools/video_tool.py \
  src/assistant_agent/tools/registry.py src/assistant_agent/agent/runtime.py \
  tests/test_realtime_video_memory.py tests/test_video_context.py
git commit -m "Add rolling realtime video memory"
```

---

### Task 3: Governed Bounded Background Observer

**Files:**
- Create: `src/assistant_agent/services/realtime_video_observer.py`
- Create: `tests/test_realtime_video_observer.py`
- Modify: `tests/test_architecture_boundaries.py`

**Interfaces:**
- Produces: `RealtimeVideoObserver.submit(frame) -> FrameProcessingResult`, async `wait_idle()` for bounded lifecycle synchronization, and async `close()`.
- Consumes: runtime `ToolRegistry`, `RealtimeVideoMemoryStore`, decoded context `VideoFrame`, and `AdaptiveKeyframeCollector`.
- Guarantees: one in-flight plus one latest pending selected keyframe; no direct Provider adapter imports or calls.

- [ ] **Step 1: Write failing governed-execution test**

Use a registry with a recording `video_understanding` tool and a real
`ActionValidator`/`ToolExecutor` seam:

```python
def _video_id() -> str:
    return "agent-service-video-observer-test"


def _decoded_frame(root: Path, *, sequence: int) -> VideoFrame:
    path = root / f"raw-{sequence}.jpg"
    path.write_bytes(b"\xff\xd8jpeg\xff\xd9")
    return VideoFrame(
        video_id=_video_id(),
        frame_id=f"frame-{sequence}",
        uri=str(path),
        sequence=sequence,
        timestamp_ms=sequence * 1000,
        fingerprint=tuple([sequence * 40] * 16),
        fingerprint_width=4,
        fingerprint_height=4,
    )


class AlwaysSelectCollector:
    def collect(self, frame: AIVideoFrame) -> KeyframeCollectionResult:
        processing = FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=frame.timestamp_seconds,
            sampled=True,
            sampling_rate=5.0,
            metrics=KeyframeChangeMetrics(keyframe_score=1.0),
            keyframe_selected=True,
            qwen_called=False,
            latency_ms=1,
            decision_reason="test",
        )
        return KeyframeCollectionResult(processing=processing, selected_frame=frame)


class RecordingVideoTool(VideoUnderstandingTool):
    def __init__(self) -> None:
        super().__init__(adapter=MockVideoUnderstandingAdapter())
        self.context_metadata: dict[str, object] = {}
        self.inputs: list[dict[str, object]] = []

    def _run(self, input: VideoUnderstandingRequest, context: ToolContext) -> ToolResult:
        self.context_metadata = dict(context.metadata)
        self.inputs.append(input.model_dump(mode="python"))
        return super()._run(input, context)


@pytest.mark.asyncio
async def test_observer_validates_and_executes_selected_frame_through_tool_boundary(tmp_path: Path) -> None:
    tool = RecordingVideoTool()
    registry = ToolRegistry()
    registry.register(tool)
    memory = RealtimeVideoMemoryStore()
    observer = RealtimeVideoObserver(
        user_id="user-1", session_id="session-1", registry=registry,
        memory_store=memory, keyframe_root=tmp_path,
        collector=AlwaysSelectCollector(),
    )

    await observer.submit(_decoded_frame(tmp_path, sequence=1))
    await observer.wait_idle()

    assert tool.context_metadata["realtime_video_observation"] is True
    assert tool.inputs[0]["video_ref"] == _video_id()
    assert memory.snapshot(_video_id()).healthy is True
    await observer.close()
```

- [ ] **Step 2: Run governed observer test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_realtime_video_observer.py::test_observer_validates_and_executes_selected_frame_through_tool_boundary -q
```

Expected: FAIL because `RealtimeVideoObserver` does not exist.

- [ ] **Step 3: Implement selection conversion, artifact retention, and governed execution**

Convert the decoded context frame without leaking fingerprint metadata:

```python
ai_frame = AIVideoFrame(
    frame_id=frame.frame_id,
    timestamp_seconds=(frame.timestamp_ms or frame.sequence * 1000) / 1000.0,
    pixels=frame.fingerprint,
    uri=frame.uri,
    width=frame.fingerprint_width,
    height=frame.fingerprint_height,
    metadata={"video_id": frame.video_id, "sequence": frame.sequence},
)
```

Copy a selected JPEG under `<keyframe_root>/<opaque-video-suffix>/` before
enqueueing it. Execute the existing tool with a synthetic internal request and
ordinary validator:

```python
request = UserRequest(
    user_id=self.user_id,
    session_id=self.session_id,
    text="Update rolling realtime video state from a selected keyframe.",
    video_ids=[item.video_id],
    metadata={"source": "realtime_video_observer"},
)
state = AgentState.from_request(request)
decision = AssistantDecision(
    type="tool_call",
    tool_name="video_understanding",
    tool_input={
        "video_ref": item.video_id,
        "frame_refs": history_refs[-2:] + [item.uri],
        "user_query": "更新当前场景、物体、人物、动作和重要变化。",
    },
)
validation = self.validator.validate(
    decision=decision, registry=self.registry, request=request, state=state
)
if not validation.accepted:
    return _validation_failure(validation)
executor = ToolExecutor(
    registry=self.registry,
    context_metadata={"realtime_video_observation": True},
)
return executor.run_tool(
    state, f"video-observation-{item.sequence}", "video_understanding",
    decision.tool_input or {}, node_name="realtime_video_observer",
)
```

Map only a successful structured tool result into the semantic store. Sanitize
failure code/message before `record_failure()`. Delete a failed selected-frame
artifact after recording failure; only successful bounded keyframes remain in
the semantic snapshot.

- [ ] **Step 4: Write failing latest-wins, nonblocking, and cleanup tests**

Add tests that gate the first Provider execution, submit three selected frames,
and assert only the first and third execute; also assert `submit()` returns before
the gate and `close()` removes all retained artifacts and memory:

```python
class BlockingVideoAdapter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.sequences: list[int] = []

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        sequence = int(Path(request.frame_refs[-1]).stem.rsplit("-", 1)[-1])
        self.sequences.append(sequence)
        self.started.set()
        assert self.release.wait(timeout=5.0)
        return VideoUnderstandingResult(
            summary=f"frame {sequence}",
            objects=[f"object-{sequence}"],
            provider="blocking-test",
            output_ref=f"provider://video/test/{sequence}",
        )


def _observer(root: Path, adapter: BlockingVideoAdapter) -> RealtimeVideoObserver:
    registry = ToolRegistry()
    registry.register(VideoUnderstandingTool(adapter=adapter))
    return RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=registry,
        memory_store=RealtimeVideoMemoryStore(),
        keyframe_root=root / "keyframes",
        collector=AlwaysSelectCollector(),
    )


@pytest.mark.asyncio
async def test_observer_keeps_one_inflight_and_latest_pending_frame(tmp_path: Path) -> None:
    gate = BlockingVideoAdapter()
    observer = _observer(tmp_path, gate)
    await observer.submit(_decoded_frame(tmp_path, sequence=1))
    assert await asyncio.to_thread(gate.started.wait, 2.0)
    await observer.submit(_decoded_frame(tmp_path, sequence=2))
    await observer.submit(_decoded_frame(tmp_path, sequence=3))

    gate.release.set()
    await observer.wait_idle()

    assert gate.sequences == [1, 3]
    assert not observer.retained_path_for(2).exists()
    await observer.close()
    assert observer.memory_store.snapshot(_video_id()) is None
    assert list(tmp_path.rglob("*.jpg")) == []
```

- [ ] **Step 5: Run queue tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_video_observer.py -q
```

Expected: latest-wins and cleanup tests FAIL until the queue worker is complete.

- [ ] **Step 6: Implement bounded worker and close semantics**

Use an `asyncio.Queue(maxsize=1)`, one worker task, and an executor future for
the synchronous tool call. On replacement, delete the pending artifact and
update the pending count. Before and after each executor future, check `closed`;
late results after close never update memory. `close()` must:

```python
self.closed = True
self._drop_pending()
if self._worker is not None:
    self._worker.cancel()
    await asyncio.gather(self._worker, return_exceptions=True)
self.memory_store.remove_video(self.video_id)
self._delete_all_owned_artifacts()
```

Keep an in-flight artifact until its executor future completes. If bounded close
waiting expires, attach a completion callback that only deletes that artifact;
it must not write semantic state.

- [ ] **Step 7: Add an architecture guard**

Extend `tests/test_architecture_boundaries.py`:

```python
def test_realtime_video_observer_uses_tool_governance_not_provider_adapters() -> None:
    source = _source("src/assistant_agent/services/realtime_video_observer.py")
    assert "ActionValidator" in source
    assert "ToolExecutor" in source
    assert "assistant_agent.providers" not in source
    assert "create_video_understanding_adapter" not in source
```

- [ ] **Step 8: Run observer and governance tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_realtime_video_observer.py tests/test_architecture_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 3**

```bash
git add src/assistant_agent/services/realtime_video_observer.py \
  tests/test_realtime_video_observer.py tests/test_architecture_boundaries.py
git commit -m "Add governed realtime video observer"
```

---

### Task 4: Agent-Service WebSocket Integration And End-To-End Memory Handoff

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_agent_service_websocket.py`
- Modify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Changes: `AgentServiceConnectionState.video_observer` owns background work.
- Changes: valid video is ingested, locally considered, then ACKed without waiting for MLLM.
- Preserves: later chat snapshots stable `video_ids`; the main LLM still autonomously chooses `video_understanding`.

- [ ] **Step 1: Write a failing WebSocket nonblocking observer test**

Monkeypatch video services so observer submission starts a blocked background
analysis but returns immediately. Send `video`, then `audio`, and assert both
ACKs arrive while the background gate remains blocked:

```python
class FakeBackgroundObserver:
    def __init__(self, *, blocked: bool) -> None:
        self.blocked = blocked
        self.submitted: list[int] = []
        self.closed = False

    async def submit(self, frame: VideoFrame) -> FrameProcessingResult:
        self.submitted.append(frame.sequence)
        return FrameProcessingResult(
            frame_id=frame.frame_id,
            timestamp_seconds=float(frame.sequence),
            sampled=True,
            sampling_rate=1.0,
            metrics=KeyframeChangeMetrics(),
            keyframe_selected=True,
            qwen_called=False,
            latency_ms=0,
            decision_reason="test",
        )

    async def close(self) -> None:
        self.closed = True


class FakeIngestion:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ingest(self, session_id, frame_index, video_hex, video_config, timestamp):
        path = self.root / "frame-1.jpg"
        path.write_bytes(b"\xff\xd8jpeg\xff\xd9")
        return VideoFrame(
            video_id="agent-service-video-test", frame_id="frame-1", uri=str(path), sequence=1
        )

    def cleanup(self, video_id: str) -> None:
        return None


def test_video_observation_does_not_block_later_media_ack(monkeypatch, tmp_path: Path) -> None:
    observer = FakeBackgroundObserver(blocked=True)
    monkeypatch.setattr(agent_ws, "_create_realtime_video_observer", lambda **_: observer)
    monkeypatch.setattr(agent_ws, "_create_video_ingestion_service", lambda: FakeIngestion(tmp_path))
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "1",
                    "contents": [{
                        "speakerNumber": "10086",
                        "videoContent": "0000000165aa",
                        "time": "2026-07-13T08:30:00Z",
                    }],
                    "videoConfig": {"codec": "H264"},
                },
            )
        )
        assert websocket.receive_json()["message"] == "videoResponse"
        websocket.send_json(
            _media_envelope(
                "audio",
                {
                    "userNumber": "10086",
                    "audioIndex": "1",
                    "contents": [{
                        "speakerNumber": "10086",
                        "audioContent": "00",
                        "time": "2026-07-13T08:30:01Z",
                    }],
                    "audioConfig": {"codec": "PCM"},
                },
            )
        )
        assert websocket.receive_json()["message"] == "audioResponse"

    assert observer.submitted == [1]
    assert observer.closed is True
```

- [ ] **Step 2: Run the WebSocket test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_service_websocket.py::test_video_observation_does_not_block_later_media_ack -q
```

Expected: FAIL because connection state has no observer and video handling never submits to one.

- [ ] **Step 3: Integrate observer creation, submission, and ordered cleanup**

Add:

```python
video_observer: RealtimeVideoObserver | None = None
```

Create the observer from the same cached runtime used by chat:

```python
def _create_realtime_video_observer(*, user_id: str, session_id: str) -> RealtimeVideoObserver:
    from assistant_agent.api import routes_agent
    runtime = routes_agent.get_assistant_runtime_app().runtime
    return RealtimeVideoObserver(
        user_id=user_id,
        session_id=session_id,
        registry=runtime.registry,
        memory_store=runtime.realtime_video_memory_store,
    )
```

After each successful `ingest()`:

```python
if state.video_observer is None:
    state.video_observer = _create_realtime_video_observer(
        user_id=user_number, session_id=session_id
    )
await state.video_observer.submit(frame)
```

In `finally`, close the observer before deleting raw context:

```python
if state.video_observer is not None:
    await state.video_observer.close()
if state.video_ingestion is not None:
    for video_id in state.video_ids:
        await asyncio.to_thread(state.video_ingestion.cleanup, video_id)
```

- [ ] **Step 4: Write a failing autonomous memory handoff test**

Extend the existing scripted native-tool path: seed a healthy snapshot for the
same `video_id`, have the fake real chat adapter call `video_understanding`, and
assert the result source is memory and the visual Provider was not called:

```python
def test_native_video_tool_call_uses_rolling_memory_without_query_provider(tmp_path: Path) -> None:
    video_id = "agent-service-video-memory-test"
    frames = InMemoryVideoContextStore(window_size=3)
    memory = RealtimeVideoMemoryStore()
    keyframe = SemanticKeyframeRecord(
        frame_id="frame-1", uri=str(tmp_path / "frame-1.jpg"), sequence=1, timestamp_ms=1000
    )
    memory.record_success(
        video_id,
        keyframe,
        VideoUnderstandingResult(
            summary="桌上有一个白色水杯。",
            objects=["白色水杯"],
            provider="background-test",
            output_ref="provider://video/background-test/frame-1",
        ),
    )

    class FailingIfCalledVideoAdapter:
        def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
            raise AssertionError(f"query-time provider called for {request.video_ref}")

    registry = create_default_registry(
        video_context_store=frames,
        realtime_video_memory_store=memory,
    )
    registry.get("video_understanding").adapter = FailingIfCalledVideoAdapter()
    chat = NativeToolChatAdapter(
        [
            native_result("video_understanding", {"video_ids": [video_id], "user_query": "识别眼前物体"}),
            final_result("眼前是一个白色水杯。"),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=registry,
        video_context_store=frames,
        realtime_video_memory_store=memory,
        chat_adapter=chat,
    )

    state = runtime.run_state(
        UserRequest(
            user_id="10086", session_id="10086", text="识别眼前物体", video_ids=[video_id]
        )
    )

    result = next(item for item in state.tool_results if item.tool_name == "video_understanding")
    assert result.data["source"] == "rolling_video_memory"
    assert state.response is not None
    assert state.response.message == "眼前是一个白色水杯。"
```

- [ ] **Step 5: Run handoff test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_native_tool_call_handoff.py -k rolling_memory -q
```

Expected: FAIL until runtime and WebSocket use the shared semantic store.

- [ ] **Step 6: Complete handoff, cleanup, and session-isolation behavior**

Ensure `PreparedChat.video_ids` remains a snapshot, observer cleanup removes
only the disconnected connection's opaque video ids, and two TestClient
connections cannot retrieve each other's snapshots. Keep final delivery and ACK
code unchanged.

- [ ] **Step 7: Run agent-service, native handoff, and delivery regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py \
  tests/test_native_tool_call_handoff.py tests/test_h264_video_ingestion.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/assistant_agent/api/agent_service_websocket.py \
  tests/test_agent_service_websocket.py tests/test_native_tool_call_handoff.py
git commit -m "Connect agent service to rolling video memory"
```

---

### Task 5: Documentation, Verification, And Operational Evidence

**Files:**
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `scripts/smoke_video_understanding.py` only if an offline inspection mode is needed for the new source metadata.

**Interfaces:**
- Documents: `videoResponse` acceptance meaning, rolling observation lifecycle, memory-first resolution, failure fallback, cleanup, profiles, and observable source/status fields.
- Verifies: no new regressions in focused and fast suites; real Provider remains opt-in.

- [ ] **Step 1: Update authoritative docs**

Document the concrete flow:

```text
videoResponse(code=0)
  = H.264 validated + JPEG/fingerprint decoded + local selection accepted
  != background visual MLLM completed

video_understanding source:
  rolling_video_memory       healthy snapshot, no query-time visual call
  recent_frame_fallback      snapshot absent/not ready/latest failed
  background_keyframe_observation  governed continuous observation
```

Document fixed bounds (`3`, `8`, and `1 pending + 1 in flight`), cleanup order,
sanitized observability, and explicit `provider_smoke`/`pilot` requirements.

- [ ] **Step 2: Run documentation and diff validation**

Run:

```bash
git diff --check -- src tests docs scripts
rg -n "rolling_video_memory|recent_frame_fallback|background_keyframe_observation" \
  docs/media-agent-service-websocket.md docs/gateway-architecture.md docs/observability-harness.md
```

Expected: no whitespace errors and all three source values documented.

- [ ] **Step 3: Run the complete focused video/Gateway slice**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_h264_video_ingestion.py tests/test_realtime_video_ai.py \
  tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py \
  tests/test_video_context.py tests/test_video_understanding_tool.py \
  tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py \
  tests/test_native_tool_call_handoff.py tests/test_gateway.py \
  tests/test_gateway_session.py tests/test_gateway_api.py -q
```

Expected: PASS.

- [ ] **Step 4: Run environment and fast regression gates**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: environment check succeeds and fast tests pass. If a known baseline
failure remains, reproduce it at the pre-feature commit before classifying it as
unrelated.

- [ ] **Step 5: Run the full test suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: no new failures relative to the documented baseline. Record exact
counts and any baseline-only failures in the final report.

- [ ] **Step 6: Do not run a real Provider without a new explicit smoke instruction**

When the user explicitly requests the real smoke, run only with
`MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke`, the already-configured local
credentials, and no committed media. Evidence must show one background
observation, a later `source=rolling_video_memory` tool result, no query-time
visual Provider call, and final delivery `sent` or `acked` according to client
capabilities.

- [ ] **Step 7: Commit docs and final verification metadata**

```bash
git add docs/media-agent-service-websocket.md docs/gateway-architecture.md \
  docs/observability-harness.md scripts/smoke_video_understanding.py
git commit -m "Document continuous keyframe video memory"
```
