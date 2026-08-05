# Langfuse-first Runtime 审计闭环实施计划

> 状态：已批准并进入实施。本文是开发阶段计划，不替代 `docs/*.md` 当前架构权威。

## 目标

建立一个只读、半自动的 AgentRuntime 审计闭环：每小时优先从 Langfuse 拉取时间窗口内全部
`assistant.turn` trace、observation 与 Score，以本地 canonical JSONL 只校验导出完整性；生成脱敏
审计 bundle，再由隔离的 Codex 进程仅生成 JSON/Markdown 报告。该流程不得调用真实 Runtime
Provider、修改代码、清理 Mem0 或写回 Langfuse。

## 统一契约

- 日常 observation 质量 Score：
  `assistant_agent.quality.response_quality`、`assistant_agent.quality.grounding`、
  `assistant_agent.quality.tool_result_quality`、`assistant_agent.quality.memory_extraction`、
  `assistant_agent.quality.memory_recall`。
- Experiment-only trace Score：`assistant_agent.quality.task_conformance`。
- `tool_use` 是跨 observation 的轨迹审计结论，第一阶段只进入 Codex 报告，不写 Langfuse Score。
- Runtime terminal、tool 成败、事件数等是 execution fact，不伪装成质量 Score。
- Score 的 `name` 只表达测量对象；来源、judge、版本、模式放 metadata。

## 实施任务

1. 在 `assistant_agent.observability.runtime_audit` 定义版本化的 trace snapshot、完整性 manifest、
   finding、bundle 与 report contract；所有产物明确 `production_mutation_allowed=false`。
2. 实现 Langfuse SDK 只读 adapter：分页列出窗口内 `assistant.turn`，逐 trace 取得完整 observations 和
   scores；网络/认证失败形成基础设施 finding，不回退成质量失败。
3. 从 `.data/graph_trace.jsonl` 只提取 trace/run、时间、terminal、event count，和 Langfuse trace ID
   对账。只有缺失/不一致 trace 才把有限的 redacted canonical events 放入 fallback evidence。
4. 实现确定性扫描：缺失 export、未闭合 terminal、Judge pending、Score 缺失/重复、低分、错误
   observation、Mem0 ingestion 异常和工具轨迹候选。Judge pending 与基础设施故障不得记为质量失败。
5. 新增 `scripts/run_runtime_audit.py`，支持 `collect`、`report`、`run`；默认重扫最近两小时，使用
   watermark 去重，并写入 `.data/runtime_audit/{state,inbox,reports}`。
6. `report` 阶段用经过凭据清理的环境执行 `codex exec --ephemeral --sandbox read-only`；Codex 不可用时
   保留 bundle 和确定性 Markdown 报告并返回可解释状态。不得把 Langfuse secret 放入 prompt 或子进程。
7. 提供 `systemd --user` service/timer 模板与操作文档；timer 每小时运行一次，网络采集和 Codex 分段，
   支持手工 dry-run。
8. 使用 `tests/tdd/runtime_audit/` 完成 RED/GREEN；运行该临时测试集及离线 CLI dry-run，不调用真实
   Langfuse、LLM、Mem0 或 Tool。

## 完成标准

- 给定 fake Langfuse 页与本地 JSONL，可稳定生成相同、脱敏、可 schema 校验的 bundle。
- Langfuse 有 trace 时不读取其完整本地 timeline；缺 trace 时仅附带受限 fallback evidence。
- Score 状态能区分 `passed/failed/pending/missing/duplicate/infrastructure_error`。
- Codex 子进程环境不含 Langfuse/OpenAI/Provider credentials，命令固定为只读、ephemeral。
- 报告明确列出覆盖率、质量问题、观测缺口、记忆问题、工具轨迹问题和人工修改建议，且不执行建议。
