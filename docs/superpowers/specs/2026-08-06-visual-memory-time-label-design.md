# 视觉历史时间标签设计

## 目标

让主 LLM 能直接理解每条视觉历史文本对应的采集时间，从而回答“什么时候、多久前、当时在哪里”等问题，
同时保持 VLM 单帧描述和 text embedding 输入不变。

## 设计

时间由系统根据 `VisualSemanticRecord.captured_at_ms` 确定，禁止要求 VLM 推断或生成时间。
`visual_memory_search` 的每条 observation 保留 `timestamp_ms`，并新增 `time_label`。`time_label` 采用
确定性相对时间加带 UTC offset 的绝对时间，例如：

```json
{
  "timestamp_ms": 1785986508927,
  "time_label": "约1分13秒前（2026-08-06 11:21:48 +08:00）",
  "text": "桌上放着一个黑色键盘、一个鼠标和两根线缆。"
}
```

相对时间以本次 Tool 执行时冻结的可信 `query_at_ms` 为基准；未来时间戳只显示绝对时间，不生成负数
相对时间。绝对时间使用运行时配置的时区，当前默认是 `Asia/Shanghai`，并显式携带 offset，避免把本机
时间误当 UTC。

`time_label` 只进入 Tool 的模型投影。它不写回 `VisualSemanticRecord`，不参与 query embedding、记录
text embedding、相似度计算或排序，也不送入后台 VLM。

## 边界与失败处理

- `captured_at_ms` 缺失时继续使用现有 `created_at_ms` 回退。
- 时间格式化必须是本地确定性逻辑，不调用 Provider，不增加网络请求。
- 主 LLM observation 的 context safety/compaction 必须保留 `time_label`；计数与 Top-K 契约保持不变。
- 本次不修改最终回答 prompt；是否在回答中使用时间仍由主 LLM根据 Tool 文本判断。

## 验证

临时 TDD 覆盖：时间标签格式、相对时间边界、Top-K observation 携带标签、context projection 保留标签，
并运行视觉历史相关测试和 core pytest。所有测试保持 mock/offline，不调用真实 Provider。
