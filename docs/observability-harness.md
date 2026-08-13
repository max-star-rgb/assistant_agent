# Observability Harness

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Runtime canonical trace 与 LangSmith native tracing 的当前权威 |
| Owns | canonical trace/local ledger、LangSmith native tracing、redaction 与观测生命周期 |
| Does not own | Gateway 协议、Provider 语义、Tool 治理、评测 Dataset 内容与发布决策 |
| 源码与 schema 入口 | `src/assistant_agent/observability/` |
| 验证入口 | `docs/authority.toml` 中 `runtime-observability.verification` |
| 相邻 authority | `docs/runtime-event-stream-architecture.md`、`docs/observability-diagnosis-runbook.md`、`evals/README.md` |

## 1. 当前边界

Runtime 产生 canonical `TraceEvent`，本地 trace store 保存最小机器事实。LangSmith tracing 由实际
compiled graph、node、LLM 与 governed Tool 调用通过 native SDK/callback 产生，不从 canonical event
重建第二棵 graph tree。

稳定关联键为 `trace_id`、`run_id`、`session_id`、`span_id` 与 `parent_span_id`。`trace_id` 在 work 开始前
分配；delivery event 与 trace event 必须共享同一次事实的时间和关联键。正文默认 redacted，只有显式本机
mock 诊断开关可记录有界内容，且不得进入报告或提交。

## 2. Canonical event 与本地持久化

`RuntimeEventPublisher` 将一个运行事实分别交付给事件流与 trace store。典型 canonical event 包括：

- `run.started`、`run.completed`、`run.failed`；
- `context.build.started/finished`；
- `llm.chat.started/finished`；
- `tool.started/finished`；
- `response.delivered` 与 `assistant.turn.summary`。

本地 JSONL 是重启后可查询的最小完整性证据。`create_server_trace_store()` 组合进程内 primary 与后台
ledger writer；持久化失败不得阻塞响应，flush/close 必须在有界 deadline 内结束并公开失败结果。

## 3. LangSmith native tracing

`ASSISTANT_AGENT_LANGSMITH_ENABLED=true` 且配置 `LANGSMITH_API_KEY` 后，Runtime 通过 LangChain/LangGraph
原生 tracing 记录实际执行树。`LANGSMITH_PROJECT` 选择项目，`LANGSMITH_ENDPOINT` 与
`LANGSMITH_WORKSPACE_ID` 可按 operator 环境覆盖。未显式启用时不得创建 client 或发起网络请求。

Release Review 与 Runtime Regression 使用独立 Experiment project，但 target 都调用生产
`AgentGraphRuntime` / actual compiled graph。Dataset、Experiment、run 与 Feedback 的详细所有权在
`evals/README.md`。Runtime 不接收外部 shadow trace identity，不创建 workflow commit observer，也不为
评测重建 canonical shadow graph。
最终 Graph capability 机器矩阵的 `Stream/Streaming Modes/Time Travel/Replay/Fork` 证据指向 actual compiled
graph 的 tracked tests；矩阵本身不发 trace、不访问 LangSmith，也不把 canonical event 或旧平台 exporter
当作 graph 执行证据。整体 retirement 状态只从已记录的机器证据加载；当前持久 operator audit 已使其
成为 `accepted`。

日常异常不由仓库定时审计。operator 在 LangSmith UI 或 SDK 中人工选择异常 trace、核实脱敏内容并沉淀为
固定 Runtime Regression Dataset；之后只通过 `evals/langsmith_runtime_regression` runner 回放。这个流程
不包含自动抓取、自动 judge、webhook 或远端写入后台任务。

## 4. 生命周期与安全

- `create_server_trace_store()` 只组合进程内 store 与本地 ledger；后台写入失败 fail-open。
- `RuntimeHost` 只拥有它创建的 Runtime 与 trace store，并保证每个资源最多关闭一次。
- ledger queue 有界；close 超时返回失败而不是无限阻塞。
- secrets、Authorization header、Provider 原始 payload、用户原文和媒体正文不得写入默认 trace。
- mock pytest、inspect 与文档校验不得启用真实 Provider、真实 LangSmith 写入或任意远端副作用。

## 5. 维护不变量

1. canonical event 是本地执行事实；任何远端视图都不得反写它。
2. LangSmith 只观察 actual graph，不从 canonical event 构造平行 graph。
3. correlation 在工作开始前存在，delivery 与 trace 可以按 `run_id`/`trace_id` 对齐。
4. ledger 失败不阻塞主响应，且 shutdown 有界。
5. 日常异常只经人工筛选后进入 LangSmith Runtime Regression Dataset。

## 6. 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/core/contract/test_observability_contract.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/langsmith-parallel-evaluation
python -m compileall -q src/assistant_agent/observability
```

修改 trace schema、publisher、本地 ledger、LangSmith tracing 装配或生命周期时，必须同步本文件与
`OBS-001`。真实 trace 诊断步骤见 `docs/observability-diagnosis-runbook.md`。
