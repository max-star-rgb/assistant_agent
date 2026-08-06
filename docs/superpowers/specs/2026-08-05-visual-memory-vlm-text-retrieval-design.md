# Visual Memory 基于 VLM 文本的基础检索设计

> 后续输出预算设计已由
> `2026-08-05-visual-memory-tool-context-compaction-design.md` 增强：Store 仍保留原始 256 条，但 Tool
> observation 不再无条件完整展开。

日期：2026-08-05

## 目标

`visual_memory_search` 不再计算或比较 query/record text embedding，也不在 Tool 内判断目标是否出现。
Tool 只读取当前可信 user/session、as-of 和时间范围内已保留的原始 `VisualSemanticRecord`，把 Store
中最多 256 条逐帧 VLM 文本一次性交给主 LLM，由主 LLM完成语义检索和最终判断。

本变更不修改语义关键帧选择、`live_view_inspect` 或 `visual_reminder_manage`。视觉提醒继续在 VLM 前
复用选中帧的 image embedding 与提醒目标 text embedding 实时匹配。

## 当前问题

现有历史检索把用户 query 和 VLM canonical text 都编码到 embedding space，再按 cosine 阈值过滤。
trace `4a67c5da235f2b8adc7ddf1abf81cb65` 中，sequence 33 的 VLM 已确认黑色智能手机，但向量检索只返回
sequence 15 的低相关水杯记录，导致主 LLM根本没有看到正确文本。

## 数据流

```text
selected keyframe
    -> realtime_video_observe
    -> VisualSemanticRecord（原始 VLM 结构化文本）
    -> SessionVisualSemanticStore

user query
    -> visual_memory_search
    -> session/as-of/time-window 过滤
    -> 按 frame_sequence 正序生成完整时间线
    -> 逐帧 VLM 文本 Tool observation
    -> 主 LLM语义检索、证据判断和回答
```

## Tool 输入契约

保留：

- `query`：保留给主 LLM和审计表达本次查找目标；Tool 不用它排序或过滤。
- `time_window`：继续支持 `lookback_seconds/start_ms/end_ms`。
- `search_mode`：为兼容现有调用保留，但基础实现不改变记录选择。

session、user 和初始 as-of sequence 仍由 Runtime/`ToolContext` 注入，模型不能覆盖。

## Tool 输出契约

```json
{
  "status": "records",
  "observations": [
    {
      "timestamp_ms": 1785914751990,
      "text": "白色桌面上放着一部黑色智能手机。"
    }
  ],
  "observation_count": 1,
  "errors": []
}
```

规则：

- `status=records`：列表至少有一条 VLM 记录。
- `status=empty`：可信范围内没有记录；这不等价于 VLM 已证明目标不存在。
- `status=unavailable`：store 不可用或读取失败。
- 不再返回 `confirmed/candidate/not_found`，因为 Tool 不负责语义判断。
- 不返回 similarity、embedding metadata、向量、evidence path、图片或 raw Provider payload。
- `observations` 与并行实现中的 live-view 时间线共用 `{timestamp_ms, text}` shape，按
  `frame_sequence/created_at_ms` 正序排列，最多包含 Store retention 内的 256 条记录。

## 输出预算与完整性

通用 Context observation compactor 默认会把 list 截为 3 项；`visual_memory_search` 必须使用专用投影，
完整保留 `observations`，不得套用该通用 list 截断。完整时间线进入实际 Provider request budgeting；若
它使请求超过 hard context，沿用现有显式 context overflow 失败语义，不得静默裁剪记录后让主 LLM回答
“没有看到”。

## Store 与索引变化

- `SessionVisualSemanticStore` 使用并行实现提供的有界 as-of 时间线读取，调用时传入 Store retention
  上限 256，并在 Tool 层应用可选时间范围。
- `has_searchable_history()` 改为判断是否存在成功 VLM record，不再要求 `index_status=ready`。
- `VisualSemanticRecord.search_embedding/embedding_space_id/index_status` 先保留为兼容字段，但
  `visual_memory_search` 不再读取它们。
- 生产 VLM 单帧输出、时间线累积和视觉提醒由当前并行变更负责；本任务不修改这些生产路径，避免覆盖
  并行工作。历史检索只消费最终 `VisualSemanticRecord.summary` 时间线。

## 失败与观测

- 查询事件只记录 `status`、返回帧数、首尾 sequence 和 latency，不记录 query 或 VLM 正文。
- as-of 和 time window 过滤必须在读取记录时完成。
- 空列表、完整列表和 store unavailable 保持可区分，避免把证据缺失描述为目标不存在。

## 测试边界

- Core invariant：unchanged；这是具体 builtin Tool 行为，不修改 `tests/core`。
- 临时 RED/GREEN 位于 `tests/tdd/visual-memory-vlm-text-search/`，用户可手动整目录删除。
- 离线测试覆盖：最多 256 条逐帧文本完整返回、无 query embedding 调用、as-of/time-window、空记录、
  敏感字段不外泄、Context 投影不把列表截成 3 条，以及 sequence 33 黑色手机文本进入实际 Tool
  observation。
- 不在 pytest 中调用真实 VLM、embedding Provider 或网络。

## 文档同步

实现完成后同步 `docs/multimodal-embedding-architecture.md` 和 `docs/context_engineering_status.md`：历史
视觉检索改为主 LLM消费分页 VLM 文本；视觉提醒仍保持 VLM 前的 text-to-image 实时匹配。
