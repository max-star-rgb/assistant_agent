# 运行审计问题 Trace 跳转设计

## 背景与目标

对话式运行审计日报已经能把多个异常归并成自然语言问题，但仅有问题描述时，维护者仍需到第三层 JSON 中手工查找具体对话。对于“品牌指代错误、历史找物证据不足、旅行信息缺少依据、咨询意向被写成确定计划”这类由多次对话共同支持的结论，日报需要提供最近的具体运行入口。

本次变更在保持正文简洁的前提下，为每个问题增加最近相关 `assistant.turn` 的编号列表。列表显示发生时间、原始 `session_id`、`trace_id`，并优先使用 Langfuse SDK 原生生成的 Trace URL，实现一键打开对应运行记录。

## 人读报告契约

每个问题继续先显示自然语言说明。其后仅在存在可认证的 Trace 证据时增加“最近的相关记录”列表：

```markdown
最近的相关记录：

1. 2026-08-06 14:20
   Session：`agent-service-...`
   Assistant turn：[`trace-id`](http://127.0.0.1:3000/project/.../traces/trace-id)
```

列表规则如下：

1. 从该问题已经通过结构化校验的 `trace_evidence_refs` 和 `runtime_verification_refs` 中提取 Trace ID；Score 或 observation 后缀不产生重复记录。
2. 只接受当前 audit bundle 中真实存在、名称为 `assistant.turn` 的 Trace。
3. 按 Trace 时间倒序排列，去重后最多展示最近 3 条。
4. 时间使用北京时间，精确到分钟。
5. `session_id` 与 `trace_id` 是“人读 Markdown 不展示机器 ID”规则的明确例外，只出现在编号定位列表中，不进入自然语言正文。
6. 同一 session 下不同 `assistant.turn` 仍分别展示，因为它们代表不同运行。
7. 内部 JSON 和 issue registry 保留全部证据；三条上限只影响 Markdown 展示。

## 链接来源与数据流

`LangfuseSdkAuditSource` 在读取每个 Trace 时调用 Langfuse Python SDK 的公开 `get_trace_url(trace_id=...)`。成功获得的 URL 随 Trace snapshot 进入完整 audit bundle，但不需要交给 Codex，也不参与问题判断。

日报发布时，Python 根据已经校验的 issue evidence ref 从完整 bundle 建立 `trace_id -> timestamp/session_id/trace_url/name` 映射，完成去重、倒序和最多三条选择，再交给 Markdown renderer。Codex 不得生成、复制或修改 session、Trace ID 与 URL，因此模型输出错误不能造成跳转串线。

Trace URL 属于本机诊断入口，必须是无 userinfo 的 `http` 或 `https` URL。renderer 对 Markdown 标签和 URL 分别做安全处理，不允许正文或外部数据注入任意链接。

## 降级与失败处理

Trace URL 获取属于只读观测增强，必须 fail-open：SDK 返回 `None`、项目 ID 查询失败或单条 URL 不合法时，不能使 Trace 收集或日报失败。对应编号仍显示时间、session 和 Trace ID，并写“Langfuse 链接暂不可用”。

如果 evidence ref 无法在当前 bundle 中找到对应 Trace，既有证据认证继续负责拒绝或降级该 issue；renderer 不自行猜测 session、时间或链接。没有可展示记录时不生成空列表。

## 长度与隐私边界

自然语言正文继续使用现有长度预算。编号定位列表不截断 `session_id` 或 `trace_id`，因此不计入 1,500 字的人读正文预算，但每个问题最多三条提供确定上界。

日报仍不得包含用户完整对话、Memory 正文、Provider 原始响应、凭据或 URL userinfo。该报告位于本机 `.data/runtime_audit/reports/`，展示原始 session ID 是本次由维护者明确批准的本地诊断例外，不改变 Gateway 日志、公开 trace summary 或其他观测面的脱敏规则。

## 验证范围

临时测试继续放在 `tests/tdd/runtime_audit`，覆盖：

- 同一问题的 Trace 按北京时间倒序、去重并只保留最近三条；
- Score/observation 组合引用只生成一条 Trace 记录；
- 每条记录显示正确 session、完整 Trace ID 和可点击 Markdown 链接；
- URL 缺失或不安全时降级为不可点击文本，不影响日报成功；
- 未被该 issue 引用的 Trace 不进入列表；
- 自然语言正文仍不泄漏其他机器 ID，内部 JSON 仍保留完整证据；
- 既有对话式表达、长度边界、日期幂等和失败保留旧报告行为不变。

## 非目标

- 不改变异常 Trace 筛选、Score 判定或 issue 归并逻辑；
- 不在报告中嵌入完整对话内容；
- 不为 session 创建新的 Langfuse 页面或查询接口；
- 不让 Codex 访问 Langfuse、网络或完整 bundle；
- 不改变 Langfuse、Memory 或生产数据。
