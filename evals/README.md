# Agent Eval 体系

`evals/` 只保存需要真实系统能力或 Agent 行为评分的评估，不属于 pytest。

```text
evals/
  system/                  # 真实能力验证；本地 Python runner 和 artifact 是权威
    common/
    tools/
    context/
    memory/
  cases/
    langfuse/              # 端到端案例；Langfuse Dataset/Experiment/Score 是权威
```

## System eval

System eval 回答“某项真实能力是否连通且经过项目治理链路”。它使用手写 Python runner、结构化硬断言
和 `.data/evals/system/` artifact，不依赖 Langfuse Dataset、Experiment 或 Evaluator。生产 OTLP 配置
存在时仍可把 trace 发送到 Langfuse，仅用于观察和排障。

所有真实运行必须显式设置：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
```

并完整配置 chat Provider 和当前场景需要的 Tool/Memory Provider。不能因检测到 key 自动运行，也不能
从 real 回退 mock。

### Tool

`evals/system/tools/cases.json` 只保存少量、可定位的真实 Tool 能力场景。执行链路为：

```text
UserRequest
  -> AgentGraphRuntime
  -> 真实 LLM 自主返回 tool call
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> 真实 Tool/MCP
  -> ToolResult
  -> 真实 LLM 最终回答
```

只检查场景和配置，不调用 Provider：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py --dry-run
```

真实执行还需要 operator 显式确认：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id weather_beijing_today
```

结果写入 `.data/evals/system/tools/<run>/`，包括 summary、每 case 结构化检查、原始 case 快照和 trace。

### Context

Context system eval 通过真实 Runtime 和真实 chat adapter 运行一轮，同时捕获：

- 项目编译出的 `ChatRequest`；
- Provider adapter 实际构造的请求 payload；
- run/trace 关联和结构化结果。

因为用户要求查看未脱敏原文，命令必须显式确认，并且只能使用合成输入：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_context_eval.py \
  --allow-unredacted-context
```

结果写入 `.data/evals/system/context/<run>/`。该目录不得提交 Git，不得使用生产用户对话、真实记忆、
联系人或媒体。

### Memory

当前 Memory 公开契约只有 completed-turn capture 和 session recall，没有 update/delete/reset。因此
暂不提供会写入真实 Mem0 的自动 runner，避免无法清理的测试数据。

当前状态和后续闭环见 `evals/system/memory/README.md`。在 delete/reset 成为正式受治理契约前，真实
Mem0 只允许在可丢弃实例中按 operator runbook 人工检查；确定性行为由
`tests/integration/memory/` 验证。

## Langfuse case eval

端到端用户任务全部进入 `evals/cases/langfuse/`：

```text
Langfuse Dataset
  -> Langfuse Experiment
  -> AgentGraphRuntime
  -> AgentExperimentOutput
  -> Langfuse Code Evaluator
  -> Langfuse Score
```

Langfuse 是 Dataset、Evaluator、Score 和 Experiment 对比的运行时权威。项目只负责执行 Agent 并
返回 Tool、policy、环境状态变化、最终回答和 trace 等结构化证据。

`agent_closed_loop_v1.seed.json` 只用于第一次创建 Dataset 或 operator 显式重置 seed。普通 Experiment
直接读取 Langfuse Dataset，不会用本地文件覆盖 UI 修改。`agent_strict_pass.ts` 是需要部署到
Langfuse Evaluators 页面的 Code Evaluator 源码。

`personal_assistant_daily.catalog.json` 保留原有 20/10/5 个人助理案例语料，作为后续导入 Langfuse
Dataset 的候选 catalog；当前 Experiment 不会自动执行它，也不存在第二套本地评分 runner。

当前 Experiment 默认使用 scripted mock，只验证 Langfuse 闭环基础设施，不代表真实模型泛化能力。
后续真实模型 case eval 仍通过同一个 Langfuse Dataset/Experiment 入口注入 real Runtime，不恢复独立
本地 case runner。

### 离线契约验证

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

只校验 seed，不连接 Langfuse：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --dry-run
```

### Langfuse 运行

第一次显式创建 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --seed-only
```

运行 Experiment：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --run-name my-agent-eval
```

命令默认从未跟踪的 `.env` 加载 Langfuse 凭据和 host。显式 Experiment 对 Dataset、Runtime OTLP trace
和 evaluator 闭环采用 fail-fast；普通生产观测仍保持 fail-open。

## 统一安全要求

- 不提交 API key、token、真实 `.env`、Provider 原始响应或真实用户数据；
- system eval 使用唯一测试身份和只读场景优先；
- 写入型场景必须具备 confirmation、独立 namespace 和可验证 cleanup；
- 缺少配置、认证失败、超时、限流或外部服务异常必须明确失败或 blocked；
- system eval artifact 写入 `.data/evals/system/`；
- case eval 的 Dataset、Experiment 和 Score 以 Langfuse 为权威；
- 本轮调用真实 Provider 时，任务总结必须报告调用范围和结果。
