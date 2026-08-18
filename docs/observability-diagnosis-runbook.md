# Observability Diagnosis Runbook

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 真实 run/trace 的机器事实诊断与证据降级当前权威 |
| Owns | trace_id 取证顺序、LangSmith/local 查询、证据降级、归因格式与敏感信息处理 |
| Does not own | trace schema、Graph 行为、Agent Server/media wire contract、评测准入 |
| 源码与 schema 入口 | `src/assistant_agent/observability/trace_query.py`、`trace_store.py`、`trajectory_debug.py` |
| 验证入口 | `docs/authority.toml` 中 `observability-diagnosis.verification` |
| 相邻 authority | [`observability-harness.md`](observability-harness.md)、[`agent-server-architecture.md`](agent-server-architecture.md)、[`../evals/README.md`](../evals/README.md) |

## 1. 查询模式与诊断顺序

### 1.1 精确 run_id 快速定位

当用户只要求定位、打开或确认某条 LangSmith 记录，并提供了合法 UUID 形式的
`run_id` 时，先走快速路径：

1. 使用当前已配置的 LangSmith client 按 UUID 直接 retrieve run，不先扫描仓库、检索本地日志、
   查询 project 列表或做时间范围搜索。
2. 命中后立即返回 root name、status、开始/结束时间、总耗时和 LangSmith 直达 URL；定位请求
   到此结束。
3. 只有用户明确要求诊断、解释或展开执行轨迹时，才加载 child runs 并核对 node、LLM、Tool、
   latency 与 Feedback。
4. 只有直接 retrieve 未命中、无权限或请求对象不是 LangSmith run ID 时，才进入下节的完整
   诊断与证据降级流程。

快速定位不读取或输出 prompt、message content、Provider payload 或 Tool 原始结果；凭据仅从本地
安全配置加载，不得在命令、输出或报告中展开 API key。

### 1.2 完整诊断与证据降级

当用户提供 `assistant.turn: <trace_id>`、真实 run、通话或机器日志时：

1. 确认环境、时间范围、timezone 与精确 `trace_id`/`run_id`，不要用用户正文做模糊检索。
2. 若该环境显式启用 LangSmith，在对应 project 中按 trace/run identity 查询 actual graph，核对 root、node、
   LLM、Tool、status、latency 与 Feedback。
3. 查询本地 canonical JSONL，确认 `run.started`、terminal、Tool 与 delivery 最小事实。
4. 涉及旧兼容入口或媒体交付时，再查本地 lifecycle/delivery JSONL，按 `session_id`、`turn_id`、`run_id` 对齐。
5. 比较各证据时间戳与缺口，再结合源码归因；远端未命中不能自动推断 Runtime 未执行。

不得为了诊断启用真实 Provider或写入远端。LangSmith 不可达、无权限或查无记录时，明确标记远端证据缺失，
继续使用本地事实；本地只保留最小字段时，不得推断不可见的 prompt、Provider payload 或模型思路。

## 2. 本地查询

默认 trace ledger 路径以启动配置为准。可通过 `TraceQueryService` 按 `trace_id` 或 `run_id` 查询，并检查：

- 是否存在 started 与唯一 terminal；
- Tool started/finished 是否共享 `tool_call_id` 与 `span_id`；
- response delivery 是否晚于 terminal，是否存在 cancel/timeout；
- observer/exporter error 是否只影响派生视图；
- event count、status 与关联 ID 是否一致。

不要把本地 ledger 当成完整正文历史。正文、API key、Authorization header、真实用户数据、Provider 原始响应
和媒体内容均不得复制到 issue、报告或聊天；必要片段先脱敏并最小化。

## 3. 归因格式

每次诊断至少说明：

- 定位：trace/run、环境、时间与证据来源；
- 事实：按时间排序的关键 event/node/Tool/terminal；
- 归因：最早可证明的失败边界，并区分 Graph、Provider、Tool、Agent Server/custom route、exporter；
- 限制：缺少的远端/本地事实以及不能据此得出的结论；
- 下一步：最小可复现或应人工沉淀的 regression case。

经人工确认且适合回归的异常，可在后续原生行为评测重建时脱敏加入固定 Dataset。当前没有 Runtime
Regression runner；不得恢复旧 Runtime facade，也不得建立自动 runtime audit、定时抓取或 webhook 替代链路。

## 4. 维护条件

viewer、query API、默认 ledger 路径、LangSmith native tracing 或证据顺序变化时更新本文件。trace schema 与
exporter 规则变化则更新 `docs/observability-harness.md`。
