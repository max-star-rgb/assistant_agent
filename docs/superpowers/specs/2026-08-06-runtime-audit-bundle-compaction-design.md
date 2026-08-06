# Runtime Audit Bundle 压缩设计

日期：2026-08-06
状态：待用户复核

## 背景与目标

当前全天 Runtime Audit bundle 约为 2.62 MB。对真实样本的结构统计显示：原始
`metadata` 聚合约 461 KB，49 份 Tool catalog 聚合约 420 KB，但实际只有 4 种不同
catalog；pretty-print 也使文件从约 1.53 MB 增长到 2.62 MB。过大的单文件输入已经导致
Codex 审计多次连接超时。

本次目标是在不丢失 Tool 可见性语义、Trace/Observation/Score 身份以及确定性 finding 的前提下，
减少落盘 bundle 和 Codex 输入。目标样本的预计体积约为 677 KB，较当前文件减少约 74%。

## 方案

采用“收集期保留、持久化前归一化”的两阶段边界：

1. Langfuse adapter 仍读取原始 metadata；collector 继续使用它识别
   `runtime_action`、Tool execution、Memory evidence 等确定性事实。
2. collector 完成所有确定性 findings 后，为持久化 bundle 创建归一化 Trace 快照；Trace、
   Observation 和 Score 的原始 `metadata` 不进入新版 bundle。
3. 只在 Trace/Observation 的 `input` 中识别值为列表的 `tools` 字段。对 catalog 生成完整
   SHA-256 内容摘要，将 catalog 保存到 bundle 顶层 `tool_catalogs`，原位置改为
   `tool_catalog_ref`。相同 catalog 只保存一次，不处理普通 Tool 业务输出中同名的 `tools` 字段。
4. inbox bundle 使用紧凑 JSON 序列化并省略 `None`；attempt、watermark、issue registry 和面向人的
   Markdown 保持当前可读格式。
5. Codex prompt 明确 `tool_catalog_ref` 的解析方式，不要求 Codex 猜测当时可见工具。

## 契约与兼容性

- 新写 bundle 使用 v2 schema，并新增 `tool_catalogs`；读取端同时接受现有 v1 bundle。
- v1 中存在的原始 metadata 仍可解析，`report --bundle <旧文件>` 保持可用。
- Tool catalog ID 使用完整 SHA-256；若同一摘要对应不同内容则拒绝生成 bundle，不静默覆盖。
- catalog 引用必须能在同一 bundle 的 `tool_catalogs` 中解析；模型校验拒绝悬空引用。
- 不改变 Langfuse 数据、canonical local event、Memory、Agent runtime 或原生 evaluator。

## 数据流

```text
Langfuse 原始快照（含 metadata、重复 tools）
  → 确定性分类与 findings
  → 删除持久化 metadata
  → tools catalog 摘要、去重、引用替换
  → v2 紧凑 bundle
  → Codex 只读审计
  → Markdown + issue lifecycle + watermark
```

## 失败处理

- metadata 分类失败仍按现有 collector 规则生成 infrastructure finding，不把缺失事实伪装成质量失败。
- catalog 不是 JSON 可序列化值、摘要冲突或悬空引用时，收集 attempt 失败且不推进 watermark。
- Codex 失败时保留已压缩 bundle 和 failed attempt，允许后续重试，不回退到复制原始 metadata。

## 验证

在 `tests/tdd/runtime_audit/` 增加临时 RED/GREEN 覆盖：

- findings 在 metadata 移除前仍能正确识别 Tool、Memory 和最终回答目标；
- 新 bundle 不落盘 Trace/Observation/Score metadata；
- 49 次相同/不同 catalog 能按内容去重并正确引用；
- 普通业务 output 中名为 `tools` 的字段不被改写；
- v1 bundle 仍能读取，v2 悬空引用被拒绝；
- bundle 写入为紧凑 JSON，结构化样本明显小于未压缩表示；
- Runtime Audit 与 Mem0/Langfuse 可视化相关临时测试继续通过。

`Core invariant: unchanged.`
