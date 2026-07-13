# Qwen Video Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicitly selected Qwen-VL implementation of the existing `video_understanding` tool and prove the real DeepSeek-to-Qwen realtime video loop with the repository `.env`.

**Architecture:** `ProviderConfig` resolves `MULTIMODAL_AGENT_VIDEO_PROVIDER=qwen` from the existing Qwen Vision key, base URL, and model settings. A focused `QwenVideoUnderstandingAdapter` converts the bounded chronological JPEG frame window into the existing `QwenVLClient` keyframe contract and maps its structured observation into `VideoUnderstandingResult`; the WebSocket observer, tool governance, rolling memory, and chat LLM remain unchanged.

**Tech Stack:** Python 3.12, Pydantic, OpenAI-compatible DashScope/Qwen-VL API, FastAPI TestClient/WebSocket, FFmpeg, pytest.

## Global Constraints

- Do not add dependencies or introduce a second Qwen credential set.
- Default `local_demo` and `offline_eval` profiles must still resolve video to `mock`.
- Real Qwen is enabled only by `provider_smoke` or `pilot` plus `MULTIMODAL_AGENT_VIDEO_PROVIDER=qwen`.
- Never fall back silently from an explicitly selected Qwen provider to Ark, Doubao, or mock.
- Send only bounded JPEG Data URLs and prompt-safe text; never send H.264, fingerprints, credentials, absolute paths, or Provider raw responses.
- Keep every external call behind `ActionValidator`, `ToolExecutor`, `ToolRegistry`, and `VideoUnderstandingTool`.
- Do not write real keys, media, responses, or smoke artifacts to the repository.

---

## File Structure

- Modify `src/assistant_agent/config.py`: recognize Qwen as a video provider and reuse Qwen Vision configuration.
- Create `src/assistant_agent/providers/qwen_video_understanding.py`: bridge `VideoUnderstandingRequest` to the existing `QwenVLClient` and map stable results.
- Modify `src/assistant_agent/video_ai/qwen/vision_client.py`: keep image Data URLs while excluding local URIs from prompt text.
- Modify `src/assistant_agent/services/video_adapter.py`: construct the Qwen video adapter when explicitly selected.
- Modify `src/assistant_agent/services/provider_config_validation.py`: report missing Qwen video credentials without network calls.
- Create `tests/test_qwen_video_understanding.py`: test chronological frame mapping, structured success, and structured failure.
- Modify `tests/unit/test_provider_config.py`: test profile gating and Qwen Vision configuration reuse.
- Modify `tests/test_provider_readiness.py`: test Qwen video readiness.
- Modify `docs/media-agent-service-websocket.md`: document the Qwen selector and smoke evidence fields.

### Task 1: Qwen Video Configuration And Adapter

**Files:**
- Modify: `src/assistant_agent/config.py`
- Create: `src/assistant_agent/providers/qwen_video_understanding.py`
- Modify: `src/assistant_agent/video_ai/qwen/vision_client.py`
- Modify: `src/assistant_agent/services/video_adapter.py`
- Modify: `src/assistant_agent/services/provider_config_validation.py`
- Create: `tests/test_qwen_video_understanding.py`
- Modify: `tests/unit/test_provider_config.py`
- Modify: `tests/test_provider_readiness.py`

**Interfaces:**
- Consumes: `QwenVLClient.understand_keyframe(current_frame, history_keyframes, previous_state_summary) -> VisionObservation`.
- Produces: `QwenVideoUnderstandingAdapter.understand_video(request) -> VideoUnderstandingResult`.
- Produces: `ProviderConfig.video_provider == "qwen"` with `video_understanding_*` values sourced from Qwen Vision configuration.

- [x] **Step 1: Write failing provider configuration and readiness tests**

Add tests equivalent to:

```python
def test_provider_config_selects_qwen_for_video_from_qwen_vision_settings() -> None:
    config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
        "MULTIMODAL_AGENT_VIDEO_PROVIDER": "qwen",
        "QWEN_VISION_API_KEY": "test-key",
        "QWEN_VISION_BASE_URL": "https://qwen.local/v1",
        "QWEN_VISION_MODEL": "qwen-vl-test",
    })
    assert config.video_provider == "qwen"
    assert config.video_understanding_api_key == "test-key"
    assert config.video_understanding_base_url == "https://qwen.local/v1"
    assert config.video_understanding_model == "qwen-vl-test"


def test_local_demo_does_not_enable_qwen_video() -> None:
    config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_VIDEO_PROVIDER": "qwen",
        "QWEN_VISION_API_KEY": "test-key",
    })
    assert config.video_provider == "mock"
```

Add readiness assertions that missing Qwen credentials produce `not_ready` with `QWEN_VISION_API_KEY`, while configured Qwen is `ready` without making a Provider call.

- [x] **Step 2: Run configuration tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_provider_config.py tests/test_provider_readiness.py -q
```

Expected: new assertions fail because `qwen` currently resolves to `mock` for video.

- [x] **Step 3: Write failing adapter contract tests**

Use an injected fake `VisionUnderstandingClient` to record current/history frames and return `VisionObservation`:

```python
def test_qwen_video_adapter_maps_ordered_frames_and_structured_result(tmp_path: Path) -> None:
    client = RecordingQwenClient()
    adapter = QwenVideoUnderstandingAdapter(_config(), client=client)
    result = adapter.understand_video(VideoUnderstandingRequest(
        video_ref="video-1",
        frame_refs=[str(tmp_path / "1.jpg"), str(tmp_path / "2.jpg")],
        user_query="识别物体",
    ))
    assert [record.uri for record in client.history] == [str(tmp_path / "1.jpg")]
    assert client.current.uri == str(tmp_path / "2.jpg")
    assert result.provider == "qwen"
    assert result.objects == ["红色方块"]
```

Also require missing frames and client errors to return structured `video_missing_frames` / Provider error results without raising or changing providers.
Add a payload test proving history images become Data URLs while the text prompt
does not contain `tmp_path` or any `KeyframeMemoryRecord.uri` value.

- [x] **Step 4: Run adapter tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_qwen_video_understanding.py -q
```

Expected: collection fails because `assistant_agent.providers.qwen_video_understanding` does not exist.

- [x] **Step 5: Implement minimal configuration selection**

Extend `VideoProviderName`, `_video_provider`, `_video_base_url`, `_video_api_key`, and `_video_model` so explicit Qwen video selection reuses:

```python
if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "qwen":
    return source.get("QWEN_VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "qwen":
    return source.get("QWEN_VISION_API_KEY") or source.get("DASHSCOPE_API_KEY")

if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "qwen":
    return source.get("QWEN_VISION_MODEL", "qwen-vl-plus")
```

Add Qwen validation to `_video_missing()` using the resolved video API key.

- [x] **Step 6: Implement the Qwen video adapter and factory selection**

Create a constructor accepting `QwenVLConfig` and an optional injected client. Convert all but the last frame to `KeyframeMemoryRecord`, use the last as current `VideoFrame`, call `understand_keyframe`, and map fields as follows:

```python
return VideoUnderstandingResult(
    summary=observation.summary,
    objects=list(observation.objects),
    people=list(observation.people),
    actions=list(observation.actions),
    events=list(observation.important_events),
    scene=observation.scene or None,
    provider="qwen",
    model=config.model,
    output_ref=f"provider://video/qwen/{safe_video_ref}",
    errors=list(observation.errors),
    latency_ms=observation.latency_ms,
)
```

Update `create_video_understanding_adapter()` to construct it only when `resolved.video_provider == "qwen"`.
Update `_keyframe_prompt()` to serialize only prompt-safe history metadata
(`frame_id`, timestamp, summary, scene, objects, and people), while
`_keyframe_refs_content()` remains responsible for converting the URI to image
content.

- [x] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_qwen_video_understanding.py tests/test_video_understanding_tool.py \
  tests/unit/test_provider_config.py tests/test_provider_readiness.py \
  tests/test_realtime_video_observer.py tests/test_native_tool_call_handoff.py -q
```

Expected: PASS with no network calls.

- [x] **Step 8: Commit Task 1**

```bash
git add src/assistant_agent/config.py \
  src/assistant_agent/providers/qwen_video_understanding.py \
  src/assistant_agent/video_ai/qwen/vision_client.py \
  src/assistant_agent/services/video_adapter.py \
  src/assistant_agent/services/provider_config_validation.py \
  tests/test_qwen_video_understanding.py tests/unit/test_provider_config.py \
  tests/test_provider_readiness.py
git commit -m "Add Qwen realtime video provider"
```

### Task 2: Documentation And Real DeepSeek-To-Qwen Smoke

**Files:**
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/superpowers/specs/2026-07-13-continuous-keyframe-video-memory-design.md`
- Create: `docs/superpowers/plans/2026-07-13-qwen-video-provider.md`

**Interfaces:**
- Consumes: explicit `provider_smoke`, `.env` DeepSeek chat configuration, and `MULTIMODAL_AGENT_VIDEO_PROVIDER=qwen`.
- Produces: sanitized runtime evidence for Qwen background observation, rolling-memory query resolution, autonomous tool use, and media delivery.

- [x] **Step 1: Document explicit Qwen selection and evidence semantics**

Document this process-scoped launch contract without keys:

```bash
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke \
MULTIMODAL_AGENT_VIDEO_PROVIDER=qwen \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py
```

State that `.env` remains unchanged, Chat and Qwen readiness must both pass, `videoResponse code=0` is not Provider completion, and accepted evidence includes only provider/model/source/latency/status plus the user-visible final response.

- [x] **Step 2: Run static and focused regression verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_h264_video_ingestion.py tests/test_realtime_video_memory.py \
  tests/test_realtime_video_observer.py tests/test_agent_service_websocket.py \
  tests/test_native_tool_call_handoff.py -q
```

Expected: all focused/fast tests pass; no real Provider call occurs in pytest.

- [x] **Step 3: Run the real opt-in smoke**

Generate two small, independently decodable H.264 I-frames outside the repository, start the server with the explicit Qwen video override, then keep one `/agent-service/v1` WebSocket open while sending the frames and asking `识别眼前物体，说明画面颜色。` Wait for the background snapshot before chat when observable; otherwise use the bounded fallback and report its source honestly.

Required evidence:

```text
runtime_profile=provider_smoke
chat_provider=deepseek
video_provider=qwen
background provider=qwen
background model=<configured Qwen model>
tool source=rolling_video_memory | recent_frame_fallback
chatResponse status=SUCCESS
websocket close code=1000
```

Do not print API keys, request bodies, Base64 media, absolute paths, or raw Provider responses.

- [x] **Step 4: Commit Task 2 after verification**

```bash
git add docs/media-agent-service-websocket.md \
  docs/superpowers/specs/2026-07-13-continuous-keyframe-video-memory-design.md \
  docs/superpowers/plans/2026-07-13-qwen-video-provider.md
git commit -m "Document Qwen realtime video smoke"
```

- [x] **Step 5: Merge locally after final verification**

Fast-forward the verified feature branch into `cqy`, rerun the focused Qwen/video tests on the merged tree, preserve unrelated user changes, then remove the owned `.worktrees/qwen-video-provider` worktree and feature branch.
