# VLM Trace 关联与本机内容展示设计

日期：2026-08-05

## 背景与目标

实时视频观察已经以独立的 Langfuse `vision.observation` trace 导出，内部包含
`vision.runtime`、`realtime_video_observe` 和 `vlm.infer`。主 Agent turn 中的
`live_view_inspect` 读取后台视觉语义缓存，不现场调用 VLM，因此不会在对应
`assistant.turn` 下生成 `vlm.infer`。

当前存在两个可用性缺口：

1. `live_view_inspect` 没有记录它实际消费的视觉语义来自哪条
   `vision.observation`，只能依赖 Session 和时间猜测。
2. `vlm.infer` 只导出状态、模型和耗时，Langfuse 中看不到经过解析的 VLM 文本结果。

本次变更目标是建立可验证的跨 trace 关联，并在本机允许内容观测时，让
`vision.observation` 和其中的 `vlm.infer` 直接展示可读的视觉文本。独立视觉 trace
和主 Agent trace 的边界保持不变。

## 方案选择

采用“独立 trace + 显式来源身份 + 本地内容 overlay”方案。

- 不把后台 VLM 强行嵌入 `assistant.turn`，避免伪造 Tool 现场调用 VLM 的因果关系。
- 不把视觉文本写入 canonical event 或 `.data/graph_trace.jsonl`，避免扩大持久化内容面。
- 复用现有本地 trace content 开关和 loopback exporter 约束；远程或禁用内容导出时只显示元数据。

## 数据与关联契约

### 视觉语义记录

每次后台视觉 observation 成功后，写入 `VisualSemanticRecord` 的同时保存以下来源身份：

- `source_vision_trace_id`：产生该视觉语义的独立 trace ID；
- `source_vision_run_id`：对应后台 observation run ID；
- `source_vlm_span_id`：对应 `vlm.infer` generation span ID；可用时记录；
- `record_id` 和 `frame_sequence` 继续作为缓存内部身份和帧定位依据。

这些字段属于 prompt-safe 关联元数据，不包含图像、用户正文或 Provider payload。失败 observation
不生成成功语义记录，也不能把上一条成功 trace 冒充为本次失败来源。

### `live_view_inspect` 投影

Tool 必须以本次实际选中的 `VisualSemanticRecord` 为准，将以下字段写入
`ToolResult.trace_summary`，而不是写入模型可见的 observation 正文：

- `source_vision_trace_id`；
- `source_vision_run_id`；
- `source_vlm_span_id`；
- `source_visual_record_id`；
- `snapshot_sequence`。

这些字段投影到 Langfuse 的 `live_view_inspect` observation output/metadata。若语义缓存尚无成功记录，
字段缺失即可，不生成虚假 ID。Tool 使用 fallback 记录时，关联必须指向实际 fallback 记录。

Langfuse 不提供由任意 trace ID 自动生成的稳定跨项目 URL，因此本次契约保证精确 ID 可复制、过滤和
API 查询，不在 Runtime 内拼接 UI URL。同一 `langfuse.session.id` 继续负责 Session 聚合。

## VLM 文本内容边界

### 捕获内容

为本地内容 overlay 增加按 `trace_id + span_id` 定位的 VLM 输出记录。只保存经过
`VisionUnderstandingResult` / `VideoUnderstandingResult` 校验和归一化后的字段：

- `summary`；
- `scene`；
- `objects`、`people`、`actions`、`events`；
- `changes`、`uncertainties`；
- `text_in_media` / `text_in_video`；
- `products`、`brands`、`colors`、`materials`、`style_tags`；
- `timestamps`、`confidence`；
- `provider`、`model`、`latency_ms`。

不捕获图片或视频字节、文件路径、媒体引用、embedding、鉴权信息、Provider 原始响应、原始请求 body
或未清洗异常。内容继续受既有字符数量和记录数量上限约束。

### 开关和导出

只有 `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT` 允许内容捕获时才写入 overlay。只有 exporter 同时启用
content 且目标为 loopback Langfuse 时，映射层才读取并导出该内容。

未满足条件时：

- `vlm.infer` 仍显示状态、Provider、模型、usage 和 latency；
- `vision.observation` 根 trace 仍显示成功或失败状态；
- `source_vision_trace_id` 等 prompt-safe 关联元数据仍可见；
- 不静默降级为把文本写入 canonical event。

## Langfuse 展示

成功 observation 的展示目标为：

```text
vision.observation
├─ vision.runtime
│  └─ output: {summary, scene, objects, people, actions, text_in_video, ...}
└─ realtime_video_observe
   └─ vlm.infer
      └─ output: {summary, scene, objects, people, actions, text_in_video, ...}
```

`vlm.infer` 必须是实际执行它的 governed Tool span 的子 generation。若当前投影把二者显示为兄弟节点，
应修正 ToolContext 的 parent span 传播，不通过映射层猜测父子关系。

根 trace output 使用同一份归一化结果的精简视图，使用户打开 trace 即可理解视觉结论；详细字段仍在
`vlm.infer` output 中。失败时只展示清洗后的错误码和固定错误文案，不展示 Provider 原始错误。

主 turn 的展示目标为：

```text
assistant.turn
└─ live_view_inspect
   └─ metadata/output:
      source_vision_trace_id=<实际消费的视觉 trace>
      source_vlm_span_id=<对应 generation>
      snapshot_sequence=<实际消费的帧序号>
```

## 实现边界

预计修改范围：

- `media/vision/observability.py`：记录 VLM span 身份和归一化本地输出；
- `observability/trace_conversation.py`：增加有界 VLM output overlay；
- `observability/otel_mapping.py`：为 `vlm.infer` 和 vision 根 trace 投影本地内容；
- `media/video/realtime_video_observer.py`：把 observation 来源身份传给语义记录；
- `media/video/semantic_store.py` 与 realtime snapshot：保留来源身份；
- `tools/plugins/builtin/media_inspection/video_branch.py`：把实际选中记录的来源身份加入 Tool trace summary；
- Tool executor/context：仅在验证表明 VLM parent 未指向实际 Tool span 时修正传播；
- `docs/observability-harness.md`：同步稳定关联和内容边界。

不改变 Provider 选择、关键帧策略、VLM 调用频率、视觉缓存查询语义、Agent prompt 或 Tool 选择逻辑。

## 错误与兼容行为

- 关联和内容捕获全部 fail-open；失败不得改变 VLM、缓存写入或主 Agent 回答。
- 旧 `VisualSemanticRecord` 没有来源字段时仍可读取，Langfuse 仅缺少关联元数据。
- overlay 在进程重启后消失，但已经导出到本机 Langfuse 的内容不受影响。
- Langfuse 导出失败不影响 canonical trace 和视觉语义缓存。
- 并发 observation 必须以每个 record 自身的来源身份关联，不能用“当前最新 trace”这类全局可变状态反推。

## 测试与验收

Core invariant 保持不变。本功能先在 `tests/tdd/vlm-trace-correlation-content/` 建立临时、显式运行的
离线 RED/GREEN 测试，不直接修改 `tests/core`。

最小验收包括：

1. VLM 成功结果在内容开关开启时写入按 span 定位的 bounded overlay；关闭时不写入。
2. OTel 映射在本地内容可用时把归一化文本放入 `vlm.infer` output，并投影精简 root output。
3. canonical `vlm.infer.finished` 和 `vision.observation.summary` 不包含视觉文本或媒体引用。
4. 后台 observation 生成的语义记录保存自己的 trace/run/span 身份。
5. `live_view_inspect` 使用最新、目标帧或 fallback 记录时，分别关联实际被选中的记录。
6. 无成功记录、失败 observation 和旧记录缺字段时不生成虚假关联。
7. `vlm.infer` 的 parent 是实际 `realtime_video_observe` Tool span。
8. 现有 VLM observability、media inspection 和 OTel mapping 定向测试保持通过。

真实 Provider 不纳入 pytest。本次实现完成后需要重启 Assistant Server，并通过一次真实视频会话只读核对：

- `assistant.turn/live_view_inspect` 能读到 `source_vision_trace_id`；
- 对应 `vision.observation/vlm.infer` 能看到归一化文本；
- 两边的 Session、frame sequence 和 trace ID 一致。

