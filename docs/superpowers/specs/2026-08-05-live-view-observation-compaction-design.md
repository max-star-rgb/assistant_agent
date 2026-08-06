# Live View Tool Observation 精简设计

## 目标

收窄 `live_view_inspect` 返回给主 LLM 的 `tool.observation`，减少重复语义和内部运行字段，
同时保留回答当前画面、判断证据新鲜度和诚实表达不可用状态所需的信息。

完整 `ToolResult.data`、`trace_summary`、Capability contract、API/trace 投影和工具执行链保持不变。
本次不调整后台视觉选帧、VLM 调用、等待上限或 as-of sequence 语义。

## 当前问题

当前 live-view 成功结果的完整 `data` 有 30 个字段；`_video_model_observation()` 虽然会删除空值，
仍把视觉事实、重复说明、Provider/媒体内部标识和 sequence 控制字段平铺给主 LLM。典型 observation
包含约 18 个非空 data 字段、约 1.1K JSON 字符。

主要冗余包括：

- `summary` 与 `description` 表达相近事实；
- `source`、`media_kind`、`media_refs`、Provider/模型归属不参与回答；
- `target_sequence` 与 `snapshot_sequence` 是运行时内部边界，LLM 只需要知道是否精确命中或回退；
- 多类视觉事实与 freshness 控制字段全部平铺，边界不清晰。

## 选定方案

只修改 `model_observation` 投影。新的模型视图使用以下结构：

```json
{
  "status": "ready | refreshing | stale | pending | failed | unavailable",
  "summary": "有界视觉摘要或不可用说明",
  "visual_facts": {
    "scene": "...",
    "objects": ["..."],
    "people": ["..."],
    "actions": ["..."],
    "events": ["..."],
    "products": ["..."],
    "brands": ["..."],
    "colors": ["..."],
    "materials": ["..."],
    "text_in_video": ["..."],
    "timestamps": [{"start_ms": 0, "end_ms": 1000, "description": "..."}],
    "style_tags": ["..."]
  },
  "confidence": 0.95,
  "freshness": {
    "observed_timestamp_ms": 1785911096034,
    "sequence_gap": 0,
    "fallback_used": false,
    "refresh_in_progress": true
  },
  "usable_visual_text": true,
  "error_code": "optional-safe-code"
}
```

投影规则：

- 所有 `None`、空字符串、空 list 和空 dict 均省略；`false` 和 `0` 必须保留。
- `summary` 使用原始视觉摘要；不可用分支继续使用当前可解释说明，不再额外输出重复的 `description`。
- `visual_facts` 只包含非空视觉事实；不可用时整个对象省略。
- `freshness` 合并模型真正需要的新鲜度信号：观察时间、sequence gap、是否 fallback、是否仍有刷新任务。
- `refresh_in_progress = in_flight or pending_count > 0`；不向模型暴露原始 pending 数量。
- `usable_visual_text` 在有可靠摘要时为 `true`，不可用分支为 `false`。
- `error_code` 仅在存在安全错误码时输出。
- 不再向主 LLM 暴露 `description`、`source`、`media_kind`、`media_refs`、`target_sequence`、
  `snapshot_sequence`、`pending_count`、`in_flight`、`errors`、Provider、模型和 `output_ref` 副本。

通用 `ToolObservation` envelope 仍负责 `tool_name/status/summary/outcome/is_complete/output_ref/error`；
本次不修改通用 observation 构造逻辑。

## 数据流与兼容性

```text
VisualSemanticRecord
  -> 完整 live-view payload（保持现状）
      -> ToolResult.data / contract / trace_summary（保持现状）
      -> live-view model projection（本次收窄）
          -> 通用 ToolObservation envelope
          -> 下一轮主 LLM
```

因此，读取完整 ToolResult 的 runtime、API、trace 和诊断消费者不受影响。唯一行为变化是下一轮主 LLM
看到的 Tool observation JSON shape；这是有意的模型上下文契约变更。

## 测试与文档

- Core invariant 不变；这是具体 builtin Tool 的模型投影变化，不修改 `tests/core`。
- 在 `tests/tdd/live-view-observation-compaction/` 添加临时 RED/GREEN 测试，验证成功与不可用投影。
- 更新已有 `tests/tdd/unified-siglip2` 中依赖旧平铺字段的断言。
- pytest 只使用 mock/local store，不读取 `.env`、不访问网络或真实 Provider。
- 更新 `docs/media-agent-service-websocket.md`，记录 LLM-facing observation 的最小字段边界。

## 后台视觉 latency 分析边界

实现后继续以 trace `855908e1a7d760e26bd40957382af6d8` 为基线，分解：

- ingress、解码与选帧；
- one-inflight/one-pending 队列等待；
- Qwen WebSocket 建连、首 delta 与完整 observation；
- semantic record 发布；
- `live_view_inspect.wait_for_sequence()` 剩余等待。

输出按收益、正确性风险和实现成本排序的优化建议。本轮不直接修改后台视觉 latency 行为。
