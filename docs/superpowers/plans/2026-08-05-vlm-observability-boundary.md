# VLM 独立可观测边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持视觉 Tool 治理入口不变，把副 VLM Provider 调用投影为独立 generation，并让后台实时视觉 observation 在 Langfuse 中拥有独立于 `assistant.turn` 的 trace。

**Architecture:** 复用现有 `VisionUnderstandingClient` 和 Provider adapter，不创建第二套视觉 service。`ToolExecutor` 向 `ToolContext` 注入只在进程内使用的 trace store 与父 Tool span；媒体 Tool 通过统一视觉观测 helper 调用 client。同步调用留在 `assistant.turn` 并嵌套于 `tool.execute`，后台 observation 使用独立 run/trace，并以 `vision.observation.summary` 触发独立批量导出。

**Tech Stack:** Python、Pydantic、现有 canonical trace、OpenTelemetry/Langfuse projection、pytest。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，pytest 不调用真实 Provider。
- 所有显式视觉 Tool 继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- trace 不记录 JPEG、媒体路径、base64、VLM 原始响应或视觉正文。
- 保留工作区已有改动，不回滚、不覆盖无关文件。

---

### Task 1: 同步视觉 Tool 的 VLM generation

**Files:**
- Create: `src/assistant_agent/media/vision/observability.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/runtime/tool_executor.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Test: `tests/tdd/vlm-observability/test_vlm_observability.py`

**Interfaces:**
- Consumes: `TraceStore.append(TraceEvent)`、`VisionUnderstandingClient.understand(request)`。
- Produces: `observe_vision_inference(call, context, capability, source, media_kind, media_count)`；canonical `vlm.infer.started/finished`。

- [x] **Step 1: Write the failing test**

```python
def test_media_tool_emits_vlm_generation_nested_under_tool_span():
    result = executor.run_tool(..., trace_store=store, trace_id=state.trace_id)
    vlm = next(event for event in store.events if event.canonical_event == "vlm.infer.finished")
    tool = next(event for event in store.events if event.canonical_event == "tool.finished")
    assert vlm.observation_name == "vlm.infer"
    assert vlm.observation_type == "generation"
    assert vlm.parent_span_id == tool.span_id
```

- [x] **Step 2: Run test to verify it fails**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/vlm-observability/test_vlm_observability.py`

Expected: FAIL because no `vlm.infer.finished` event exists.

- [x] **Step 3: Write minimal implementation**

```python
class ToolContext(BaseModel):
    trace_store: Any | None = Field(default=None, exclude=True)
    parent_span_id: str | None = Field(default=None, exclude=True)
```

`observe_vision_inference` 使用同一 span ID 发出 started/finished；仅记录 capability、source、media_kind、media_count、prompt_version、provider、model、latency、status 和安全错误码。

- [x] **Step 4: Run test to verify it passes**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/vlm-observability/test_vlm_observability.py`

Expected: PASS。

### Task 2: 后台视觉 observation 独立 trace

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Test: `tests/tdd/vlm-observability/test_vlm_observability.py`

**Interfaces:**
- Consumes: Task 1 的 VLM events 与 observer 自有 `AgentState.run_id/trace_id`。
- Produces: `vision.observation.summary` terminal event；Langfuse trace name `vision.observation`。

- [x] **Step 1: Write the failing test**

```python
def test_background_vision_batch_uses_vision_trace_name():
    specs = build_text_otel_span_specs(background_events)
    assert specs[0].attributes["langfuse.trace.name"] == "vision.observation"
    assert next(span for span in specs if span.name == "vlm.infer").parent_span_id == tool_span_id
```

- [x] **Step 2: Run test to verify it fails**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/vlm-observability/test_vlm_observability.py`

Expected: FAIL because mapping still labels every batch `assistant.turn` and exporter only flushes assistant summaries.

- [x] **Step 3: Write minimal implementation**

让 realtime observer 把共享 trace store 传给内部 `ToolExecutor`，执行后追加 prompt-safe `vision.observation.summary`。OTel observer 把该事件视为批量终点；mapping 根据稳定 `trace_kind=vision_observation` 输出 `vision.observation` trace，不生成 assistant turn 语义。

- [x] **Step 4: Run test to verify it passes**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/vlm-observability/test_vlm_observability.py`

Expected: PASS。

### Task 3: 文档与最终验证

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: Task 1-2 的 canonical event 与 Langfuse trace 契约。
- Produces: 当前架构权威说明和可复现验证记录。

- [x] **Step 1: Update authority docs**

明确 VLM 是 Tool 内部 Provider-neutral 能力，区分同步 Tool 子 generation 与后台 `vision.observation` trace，并列出禁止投影的媒体内容。

- [x] **Step 2: Run focused verification**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/vlm-observability`

Expected: PASS。

- [x] **Step 3: Run affected existing suites**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2 tests/tdd/realtime-visual-latency-p0`

Expected: PASS；若现有工作区改动导致无关失败，记录具体证据并缩小归因。
