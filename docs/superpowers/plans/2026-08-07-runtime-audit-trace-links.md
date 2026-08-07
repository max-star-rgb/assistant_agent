# 运行审计问题定位链接实施计划

> **执行要求：** 按任务顺序逐项实施并在每个检查点验证，不扩大到运行审计之外的观测链路。

**目标：** 在每日中文审计报告的每类问题下，列出最多 3 条最近的真实 `assistant.turn`，包含时间、`session_id`、Trace ID 和可点击的 Langfuse 链接，方便人工复核。

**实现边界：** Langfuse SDK 采集阶段保存其原生 `get_trace_url()` 返回值；Codex 仍只负责问题判断和通俗描述，不接触或生成链接。Python 渲染阶段使用问题的已验证证据引用匹配当前 bundle 中的 trace，去重、倒序并截断。链接缺失或不安全时降级为纯 ID，不阻断审计。

**技术栈：** Python 3.12、Pydantic、Langfuse Python SDK、pytest。

---

## 任务 1：用测试定义采集与渲染契约

**修改文件：**

- `tests/tdd/runtime_audit/test_runtime_audit.py`
- `tests/tdd/runtime_audit/test_daily_runtime_audit.py`

1. 增加 Langfuse 原生 Trace URL 被写入快照的测试。
2. 增加 URL 获取异常时采集继续、URL 留空的测试。
3. 增加报告按时间从近到远、同一 trace 去重、每个问题最多 3 条的测试。
4. 增加只接受当前 bundle 中真实 `assistant.turn` 的测试。
5. 增加 URL 缺失或不安全时显示“链接暂不可用”且不生成 Markdown 链接的测试。
6. 运行新增测试，确认因生产代码尚未支持而失败（RED）。

## 任务 2：保存 Langfuse 原生链接

**修改文件：**

- `src/assistant_agent/observability/runtime_audit/models.py`
- `src/assistant_agent/observability/runtime_audit/langfuse_source.py`

1. 为 `LangfuseTraceSnapshot` 增加向后兼容的可选 `trace_url` 字段。
2. 采集每条 trace 时调用 SDK 公共方法 `get_trace_url(trace_id=...)`。
3. 对缺失方法、调用异常和非字符串结果统一降级为 `None`，不影响 trace 与 score 采集。
4. 运行采集测试确认通过（GREEN）。

## 任务 3：生成可定位的问题记录列表

**修改文件：**

- `src/assistant_agent/observability/runtime_audit/report.py`
- `src/assistant_agent/observability/runtime_audit/daily_runner.py`

1. 从问题的 `trace_evidence_refs` 与 `runtime_verification_refs` 提取 trace ID，并去掉 observation/score 后缀。
2. 仅匹配本次 bundle 中名称为 `assistant.turn` 的 trace。
3. 按 trace 时间倒序排序，每个问题保留最近 3 条。
4. 使用北京时间显示到分钟，并分别输出 Session 与 Assistant turn。
5. 仅接受无凭据的 `http/https` URL；安全链接渲染为可点击项，否则显示降级提示。
6. 将 bundle trace 索引传给每日报告渲染器；没有匹配 trace 时不展示空列表。
7. 运行渲染及 daily runner 测试确认通过。

## 任务 4：同步说明并做完整验证

**修改文件：**

- `docs/observability-harness.md`
- `scripts/README.md`

1. 说明人类报告通常隐藏技术 ID，但问题定位列表明确例外。
2. 说明链接由 Langfuse SDK 原生生成，失败时审计继续。
3. 运行 `tests/tdd/runtime_audit` 全量测试、相关静态检查和 diff 检查。
4. 只提交本任务文件，不包含工作区已有的其他未跟踪计划。

## 任务 5：真实生成并验收一份报告

1. 强制重跑 `2026-08-06` 的每日审计，允许真实读取 Langfuse 并调用自动化 Codex。
2. 检查报告中每类问题最多 3 条、最近优先、Session 与 Trace ID 正确、链接可点击。
3. 检查技术 ID 只出现在明确的问题定位列表中。
4. 再次运行同日期非强制命令，确认不会重复生成。
