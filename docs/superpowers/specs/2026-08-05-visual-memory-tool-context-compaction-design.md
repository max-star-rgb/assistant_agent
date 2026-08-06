# Visual Memory Tool 尾部上下文压缩设计

日期：2026-08-05

## 目标与边界

独立 VLM client 逐关键帧处理当前图片，生成带时间戳的单帧文本；VLM 推理不读取其他帧文本，也不承担
历史压缩。`SessionVisualSemanticStore` 继续保留最多 256 条原始记录。

压缩发生在 `visual_memory_search` 读取 Store 之后、构造面向主 LLM 的 `model_observation` 之前。Tool
尾部增加视觉时间线专用压缩器，使用与 `llm.context` 相同的 `target / trigger / hard` 控制模型。主
`ContextService.preflight` 仍负责完整 Provider request 的第二级全局预算。

## 数据流

```text
关键帧 -> 独立 VLM client -> {timestamp_ms, text} -> Store 原始 256 条 retention
                                                    |
用户查询 -> visual_memory_search -> 可信 as-of/time window -> VisualTimelineContextService
                                                    |
                        below trigger: 原始列表     |
                        trigger: 旧记录压缩 + 相关原文 + 最近原文
                        hard 且无法收敛: context_hard_limit
                                                    |
                                             Tool model_observation
                                                    |
                                       主 llm.context 全局 preflight
```

## 控制模型

视觉时间线使用主 ChatAdapter 对应的 tokenizer，以及独立的视觉 Tool 输出预算。当前复用已有
`visual_context_*` 配置中的 input limit、target/trigger/hard ratio、safety margin、summary max tokens
和 keep-recent 数量；控制语义与 `ContextWindowPolicy` 完全一致。

- 低于 trigger：不调用压缩模型，完整返回全部记录。
- 达到 trigger：把最近 `keep_recent_records` 条保留为原文；旧 prefix 交给 query-aware compactor。
- target 是压缩目标。压缩后必须重建最终 Tool projection，并用同一 tokenizer 重新计数。
- 低于 hard 的压缩失败允许返回未压缩列表，并明确记录 `failed_below_hard`。
- 位于 hard 区间时最多重试一次更小 summary budget；仍失败或仍位于 hard 时返回
  `visual_memory_context_hard_limit`，不得把超限原文发给主 LLM。

## 专用压缩契约

不复用 conversation summary schema。压缩模型接收当前 `query` 和待覆盖的旧时间线，只返回：

```json
{
  "summary": "按时间概括旧画面中的可检索事实",
  "relevant_observation_indexes": [3, 18]
}
```

indexes 由本地 validator 校验后映射回原始记录，模型不能改写时间戳或证据文本。最终 Tool observation
包含：

- `observations`：相关旧原文与最近原文的去重、时间正序列表；
- `timeline_summary`：旧 prefix 的压缩摘要；
- `coverage`：source/covered/returned count、起止时间和固定 digest；
- `compaction`：状态、token 计数、阈值决策、attempts 和 target 是否达到；
- `observation_count`：可信范围内的原始记录总数；
- `returned_observation_count`：实际发送给主 LLM 的原文数。

`status=records` 仍只表示存在视觉历史，不表示目标出现。最终检索和回答由主 LLM 完成。

## 安全与失败

- Store 原文、evidence 和 embedding 状态不因压缩改变；压缩结果不反写 Store。
- Tool 不输出路径、图片、向量或 raw Provider payload。
- compactor 只可选择原始 indexes；越界、重复、非整数 index、非法 JSON 或超出 summary token budget
  均视为压缩失败。
- mock/off 模式不调用真实 Provider；未配置 Tool compactor 时保留当前原始输出，并由全局 preflight
  兜底。
- 观测只记录计数、token、阈值状态、attempts、coverage digest 和 latency，不记录 query 或 VLM 正文。

## 测试边界

Core invariant：unchanged。临时 RED/GREEN 放在
`tests/tdd/visual-memory-vlm-text-search/`，可由用户手动整目录删除。覆盖 below-trigger 零压缩、
trigger 压到 target、query 与旧记录进入 compactor、精确 index 映射、最近原文保留、hard 失败阻断、
ToolResult 契约和全局 Context 投影不二次截断。
