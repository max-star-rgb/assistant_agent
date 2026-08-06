# VLM Trace Correlation and Local Content Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `live_view_inspect` 精确关联其消费的 `vision.observation`，并让本机 Langfuse 的 `vlm.infer` 与 vision 根 trace 展示归一化 VLM 文本。

**Architecture:** 后台 VLM trace 继续独立运行；每条成功视觉语义记录携带来源 trace/run/span 身份，主 turn 的缓存读取 Tool 只投影这些 prompt-safe 关联字段。VLM 文本进入现有进程内 trace content overlay，并且只在本地内容开关与 loopback exporter 同时允许时进入 Langfuse；canonical event 永远只保留元数据。

**Tech Stack:** Python 3.12、Pydantic、现有 canonical TraceStore、OTel/Langfuse 投影、pytest offline mock。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；pytest 不调用真实 Provider 或网络。
- 不安装新依赖，不改 Provider 选择、关键帧策略、VLM 调用频率、Agent prompt 或 Tool 选择逻辑。
- 不把视觉文本、媒体路径、媒体字节、embedding 或 Provider 原始 payload 写入 canonical event/JSONL。
- 内容捕获必须同时受 `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT` 和 loopback exporter 约束，并保持 fail-open。
- 并发 observation 只能使用各自 record 保存的来源身份，禁止从全局“最新 trace”反推。
- Core invariant: unchanged；测试只放在可手动整目录删除的 `tests/tdd/vlm-trace-correlation-content/`。
- 当前 worktree 存在用户未提交改动；每个任务只检查本任务 diff，最终完成时再判断是否适合提交，不机械提交设计/计划文档。

## File Structure

- Create: `tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py` — VLM overlay、映射和 canonical 安全 RED/GREEN。
- Create: `tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py` — 后台来源身份与 Tool 关联 RED/GREEN。
- Modify: `src/assistant_agent/observability/trace_conversation.py` — 新增有界 `TraceVlmOutput` overlay。
- Modify: `src/assistant_agent/media/vision/observability.py` — 生成 trace link 并捕获归一化 VLM 输出。
- Modify: `src/assistant_agent/observability/otel_mapping.py` — 投影 VLM output、vision root output 和 Tool 关联元数据。
- Modify: `src/assistant_agent/media/video/semantic_store.py` — 在成功视觉记录中保存来源身份。
- Modify: `src/assistant_agent/media/video/realtime_video_memory.py` — 在实时快照中保留实际来源身份。
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py` — 把本次后台 Tool/VLM trace link 传给语义记录。
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py` — 传播 VLM link，并让缓存 Tool 输出实际记录关联。
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py` — 图片/视频 VLM Tool 保留本次推理 link。
- Modify: `docs/observability-harness.md` — 同步稳定关联、内容和安全边界。

---

### Task 1: 有界 VLM 内容 Overlay

**Files:**
- Create: `tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py`
- Modify: `src/assistant_agent/observability/trace_conversation.py`
- Modify: `src/assistant_agent/media/vision/observability.py`

**Interfaces:**
- Produces: `TraceVlmOutput(span_id: str, provider: str | None, model: str | None, normalized_result: dict[str, Any])`。
- Produces: `InMemoryTraceConversationStore.append_vlm_output(...)` 与 `get(..., include_vlm_outputs=True)`。
- Produces: `VisionInferenceTraceLink(trace_id: str, run_id: str, span_id: str)` 和可选 `trace_link_callback`。
- Consumes: 现有 `local_trace_content_enabled()` 和 `get_default_trace_conversation_store()`。

- [ ] **Step 1: 写入 overlay 的 RED 测试**

```python
def test_vlm_result_is_captured_only_in_local_content_overlay(monkeypatch):
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    store = InMemoryTraceConversationStore(max_records=4)
    monkeypatch.setattr(
        "assistant_agent.media.vision.observability.get_default_trace_conversation_store",
        lambda: store,
    )
    trace = InMemoryTraceStore()
    context = ToolContext(
        run_id="vision-run",
        trace_id="1" * 32,
        trace_store=trace,
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )
    result = VideoUnderstandingResult(
        summary="桌面上有一只杯子。",
        scene="室内桌面",
        objects=["杯子"],
        output_ref="mock://vision/1",
        provider="mock",
        model="mock-vlm",
    )

    observe_vision_inference(
        lambda: result,
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
    )

    view = store.get(
        user_id="user-vlm",
        session_id="session-vlm",
        trace_id="1" * 32,
        include_vlm_outputs=True,
    )
    assert view is not None
    assert view.vlm_outputs[0].normalized_result["summary"] == "桌面上有一只杯子。"
    assert "output_ref" not in view.vlm_outputs[0].normalized_result
    assert "桌面上有一只杯子。" not in str([e.model_dump(mode="json") for e in trace.events])
```

- [ ] **Step 2: 运行测试并确认因缺少 `TraceVlmOutput`/`include_vlm_outputs` 失败**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py
```

Expected: FAIL，失败点是新 overlay API 或 `vlm_outputs` 尚不存在，而不是 fixture/import 错误。

- [ ] **Step 3: 实现 bounded VLM overlay 与显式 allowlist**

在 `trace_conversation.py` 增加：

```python
class TraceVlmOutput(BaseModel):
    span_id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    normalized_result: dict[str, Any]
```

为 `TraceConversationView`、`TraceConversationRecord` 增加 `vlm_outputs`，所有现有 upsert 方法原样保留该 tuple；新增：

```python
def append_vlm_output(
    self,
    *,
    user_id: str,
    session_id: str,
    trace_id: str,
    vlm_output: TraceVlmOutput,
) -> None:
    # 同 span_id 替换，最多保留最后 16 条。
```

`get()` 增加 `include_vlm_outputs: bool = False`。在 `media/vision/observability.py` 增加：

```python
class VisionInferenceTraceLink(BaseModel):
    model_config = ConfigDict(frozen=True)
    trace_id: str
    run_id: str
    span_id: str

_VLM_OUTPUT_FIELDS = (
    "summary", "scene", "objects", "people", "actions", "events",
    "changes", "uncertainties", "text_in_media", "text_in_video",
    "products", "brands", "colors", "materials", "style_tags",
    "timestamps", "confidence", "provider", "model", "latency_ms",
)
```

`observe_vision_inference()` 增加 `trace_link_callback: Callable[[VisionInferenceTraceLink], None] | None = None`；span 创建后 fail-open 通知 link，成功后仅在内容开关开启且身份完整时把 allowlist 结果写入 overlay。

- [ ] **Step 4: 增加关闭开关、敏感字段和 callback fail-open 测试并跑绿**

断言关闭 `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=0` 时 `view is None`；callback 抛异常时 Provider 结果仍返回；`output_ref/media_refs/frame_refs/raw_provider_payload` 不进入 overlay。

Run: 与 Step 2 相同。Expected: PASS。

- [ ] **Step 5: 检查 Task 1 diff**

Run:

```bash
git diff --check -- \
  src/assistant_agent/observability/trace_conversation.py \
  src/assistant_agent/media/vision/observability.py \
  tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py
```

Expected: 无输出。

### Task 2: Langfuse VLM 与 Vision Root Output

**Files:**
- Modify: `tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py`
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`

**Interfaces:**
- Consumes: Task 1 的 `TraceConversationView.vlm_outputs`。
- Produces: `_vlm_output_for_event(conversation, span_id)`。
- Produces: vision root 与 `vlm.infer` 的 `langfuse.observation.output`/`langfuse.trace.output`。

- [ ] **Step 1: 写 VLM generation 和 root output RED 测试**

```python
def test_vision_mapping_exports_normalized_vlm_text_from_overlay():
    events = background_vision_events(trace_id="2" * 32, vlm_span_id="vlm-span")
    conversation = TraceConversationView(
        trace_id="2" * 32,
        user=TraceConversationText(text="", chars=0),
        assistant=TraceConversationText(text="", chars=0),
        vlm_outputs=[TraceVlmOutput(
            span_id="vlm-span",
            provider="mock",
            model="mock-vlm",
            normalized_result={"summary": "窗边有一盆绿植。", "objects": ["绿植"]},
        )],
    )
    specs = build_text_otel_span_specs(events, conversation=conversation)
    root = next(item for item in specs if item.name == "vision.runtime")
    vlm = next(item for item in specs if item.name == "vlm.infer")
    assert json.loads(vlm.attributes["langfuse.observation.output"])["summary"] == "窗边有一盆绿植。"
    assert json.loads(root.attributes["langfuse.trace.output"])["summary"] == "窗边有一盆绿植。"
```

- [ ] **Step 2: 运行单测并确认当前 output 只有 status/content_exported**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py::test_vision_mapping_exports_normalized_vlm_text_from_overlay
```

Expected: FAIL，`summary` 缺失。

- [ ] **Step 3: 让 exporter 读取 VLM overlay 并在映射层按 span 合并**

`TextOtelTraceObserver._trace_conversation()` 调用 `get(..., include_vlm_outputs=True)`。在 mapping 中：

```python
def _vlm_output_for_event(conversation, *, span_id: str | None) -> TraceVlmOutput | None:
    if conversation is None or not span_id:
        return None
    return next((item for item in reversed(conversation.vlm_outputs) if item.span_id == span_id), None)
```

当 `name == "vlm.infer.finished"` 时，用匹配的 `normalized_result` 作为 generation output；vision root 从最后一个成功 `vlm.infer.finished` 的 span 读取精简结果。无 overlay 时继续输出 `{status, content_exported: false}`。

- [ ] **Step 4: 增加无 overlay、失败和媒体引用不泄露测试并跑绿**

测试必须解析 JSON 后断言结构：无 overlay 不含 `summary`；失败 VLM 不借用上一条成功 output；输出不含 `output_ref/media_refs/frame_refs`。

Run: Task 1 全文件命令。Expected: PASS。

- [ ] **Step 5: 检查 Task 2 diff**

Run:

```bash
git diff --check -- \
  src/assistant_agent/observability/otel_exporter.py \
  src/assistant_agent/observability/otel_mapping.py \
  tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py
```

Expected: 无输出。

### Task 3: 后台视觉记录保存自身 Trace Link

**Files:**
- Create: `tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py`
- Modify: `src/assistant_agent/media/video/semantic_store.py`
- Modify: `src/assistant_agent/media/video/realtime_video_memory.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py`

**Interfaces:**
- Consumes: Task 1 的 `VisionInferenceTraceLink` 与 callback。
- Produces: `VisualSemanticRecord.source_vision_trace_id/source_vision_run_id/source_vlm_span_id`。
- Produces: `RealtimeVideoSnapshot.source_vision_trace_id/source_vision_run_id/source_vlm_span_id/source_visual_record_id`。

- [ ] **Step 1: 写后台 record 身份 RED 测试**

复用 offline `RealtimeVideoObserver + FakeRealtimeVisionAdapter`，等待一次 observation 后断言：

```python
record = observer.semantic_store.latest("video-vlm")
vlm = next(e for e in trace_store.events if e.canonical_event == "vlm.infer.finished")
summary = next(e for e in trace_store.events if e.canonical_event == "vision.observation.summary")
assert record is not None
assert record.source_vision_trace_id == summary.trace_id
assert record.source_vision_run_id == summary.run_id
assert record.source_vlm_span_id == vlm.span_id
assert vlm.parent_span_id == next(e.span_id for e in trace_store.events if e.canonical_event == "tool.finished")
```

- [ ] **Step 2: 运行测试并确认来源字段不存在**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py::test_background_record_retains_its_own_trace_link
```

Expected: FAIL，缺少 `source_vision_trace_id` 等字段。

- [ ] **Step 3: 传播 trace link 到后台 ToolResult 和成功 record**

在 video branch 的 VLM 调用中用 callback 捕获 `VisionInferenceTraceLink`，并合并到
`ToolResult.trace_summary`：

```python
{
    "source_vision_trace_id": link.trace_id,
    "source_vision_run_id": link.run_id,
    "source_vlm_span_id": link.span_id,
}
```

observer 从当前 `ToolResult.trace_summary` 读取并校验字符串，不使用全局状态；
`_publish_visual_semantic_record()` 接收该 link 并写入 `VisualSemanticRecord`。三个新字段均为可选，保证旧构造代码兼容。

- [ ] **Step 4: 把实际 record 来源投影进 realtime snapshot**

`RealtimeVideoSnapshot` 增加四个可选字段；`RealtimeVideoMemoryStore.record_success()` 和
`_project_visual_semantic_snapshot()` 从当前成功 record 写入来源，out-of-order/fallback 时保留被选中 record 的来源，不覆盖为最新全局值。

- [ ] **Step 5: 增加并发顺序和失败 observation 测试并跑绿**

用两个不同 trace sentinel 的 record 验证按 `target_sequence` 取较早 record 时仍返回较早 trace；失败 observation 不创建新的成功来源。

Run: Task 3 全文件命令。Expected: PASS。

### Task 4: `live_view_inspect` 跨 Trace 关联投影

**Files:**
- Modify: `tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`

**Interfaces:**
- Consumes: Task 3 的 snapshot 来源字段。
- Produces: Tool `trace_summary` 中的 `source_vision_trace_id/source_vision_run_id/source_vlm_span_id/source_visual_record_id/snapshot_sequence`。
- Produces: Langfuse Tool observation 顶层 metadata 与 output 中的同名关联字段。

- [ ] **Step 1: 写 Tool 关联 RED 测试**

准备 semantic store 中 target sequence 记录，运行 governed `live_view_inspect`，再映射 OTel：

```python
tool = next(item for item in specs if item.name == "live_view_inspect")
output = json.loads(tool.attributes["langfuse.observation.output"])
assert output["source_vision_trace_id"] == "a" * 32
assert output["source_vlm_span_id"] == "vlm-source-span"
assert output["snapshot_sequence"] == 7
assert tool.attributes[
    "langfuse.observation.metadata.assistant_agent.source_vision_trace_id"
] == "a" * 32
```

- [ ] **Step 2: 运行测试并确认 metadata-only Tool 当前丢弃这些字段**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py::test_live_view_tool_projects_exact_source_trace_link
```

Expected: FAIL，Tool output/metadata 缺少来源字段。

- [ ] **Step 3: 在 Tool trace summary 和 OTel mapping 中加入 allowlisted 关联**

`VideoUnderstandingBranch._trace_summary()` 从 snapshot 输出五个来源字段。mapping 增加一个只解析
`tool.finished/tool.failed` 的 helper，从 boundary `output_summary.observation_summary.trace_summary`
读取固定字段；把它们加入 `langfuse.observation.output`，并以
`langfuse.observation.metadata.assistant_agent.<field>` 投影。不得开放任意 trace_summary key。

- [ ] **Step 4: 增加 latest、target、fallback、unavailable 四种测试并跑绿**

分别断言关联指向实际 record；unavailable 不含来源字段；metadata-only 仍不包含视觉 summary、媒体 ref 或 Tool model observation。

Run: Task 3 全文件命令。Expected: PASS。

- [ ] **Step 5: 检查 Task 3–4 diff**

Run:

```bash
git diff --check -- \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/video/realtime_video_memory.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py \
  src/assistant_agent/observability/otel_mapping.py \
  tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py
```

Expected: 无输出。

### Task 5: 权威文档与定向回归

**Files:**
- Modify: `docs/observability-harness.md`
- Verify: `tests/tdd/vlm-trace-correlation-content/`
- Verify: `tests/tdd/vlm-observability/`
- Verify: 受影响 media/observability 定向测试文件。

**Interfaces:**
- Consumes: Tasks 1–4 的最终字段与安全边界。
- Produces: 当前架构权威说明和验证证据。

- [ ] **Step 1: 更新 observability authority**

在 `Langfuse、OTel 与评估投影` 章节明确：视觉语义 record 保存来源 trace/run/span；
`live_view_inspect` 只投影实际消费 record 的 prompt-safe 关联；VLM 归一化文本只在本地内容 overlay + loopback exporter 下展示，canonical JSONL 不包含该文本。

- [ ] **Step 2: 运行新 feature TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-trace-correlation-content
```

Expected: 全部 PASS。

- [ ] **Step 3: 运行既有 VLM observability 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/vlm-observability
```

Expected: 全部 PASS，并继续证明 canonical 不含媒体/视觉文本、Tool/VLM parent 真实。

- [ ] **Step 4: 根据 import/定向失败确定最小相邻回归**

先用 `rg` 找到直接覆盖 `semantic_store.py`、`video_branch.py`、`trace_conversation.py` 和
`otel_mapping.py` 的现有 TDD 文件，只运行这些显式路径；不得因为任务结束机械运行裸 pytest。

- [ ] **Step 5: 最终安全与格式检查**

```bash
git diff --check
rg -n "raw_provider|frame_refs|media_refs|output_ref" \
  src/assistant_agent/media/vision/observability.py \
  src/assistant_agent/observability/trace_conversation.py
```

人工核对匹配项只存在于排除/allowlist 逻辑和测试 sentinel，不在 canonical output 写入路径。

- [ ] **Step 6: 重启后的只读真实 trace 验收**

由 operator 重启 Assistant Server 并触发一次实时视频会话后，使用本机 Langfuse read API 核对新的
`assistant.turn` 和对应 `vision.observation`。只读取 trace/observation metadata 和已允许的归一化视觉文本；不触发额外真实 Provider 调用。

- [ ] **Step 7: 最终 diff ownership 与提交判断**

只列出本任务相关文件和 hunk；若与用户已有改动重叠到无法安全分离，则不提交并在总结中说明。若可安全归属，提交只包含本任务代码、测试和 authority 文档，不提交 `docs/superpowers/**` 开发材料。

