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
  -> Langfuse Code Evaluator + LLM-as-a-Judge
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
`agent_real_readonly_v1.seed.json` 是第一批真实模型 Dataset seed，包含 2 个 no-tool 和 3 个真实天气
案例。它仍通过同一个 Langfuse Dataset/Experiment 入口注入 real Runtime，不恢复独立本地 case
runner。真实 profile 强制关闭 Mem0、持久化会话、checkpointer 和 durable task，并要求
`--allow-real-tools`，避免把 scripted 日历写入 Dataset 误用于真实环境。

`agent_strict_pass.ts` 同时支持 scripted baseline 和 real-readonly Dataset，并为每个 Dataset item
产生 Langfuse 原生 `agent.execution_pass`、`agent.tool_selection_pass`、
`agent.forbidden_tool_pass`、`agent.tool_execution_pass`、
`agent.response_contract_pass` 和 `agent.strict_pass`。动态天气建议的语义质量不伪装成确定性
grounding 分数；`response_contract_pass` 会拒绝 Provider
`error/refusal/truncated/empty` 结果，避免把 Runtime 的降级提示当作有效回答。后续语义质量应由
Langfuse 原生 LLM-as-a-Judge Evaluator 单独评估。

真实只读 Dataset 额外使用从 Langfuse managed `Helpfulness` 克隆的项目级
`assistant-agent-answer-helpfulness-zh` Evaluator，生成数值 Score
`agent.answer_helpfulness`。它只判断最终文本是否清晰、相关且有帮助，不替代六个确定性闭环
Score，也不证明回答中的动态天气事实绝对正确。裁判理由约束为中文且不超过 20 个汉字。变量映射为：

```text
query      <- Experiment Input，JSONPath $.user_request.text
generation <- Experiment Output，JSONPath $.response.message
```

Evaluator target 必须选择 `Experiments / Experiment Runner SDK`，filter 只匹配
`assistant-agent-real-readonly-v1`，sampling 为 `100%`。因此真实 Dataset 每个新案例最终应有
六个确定性 Score 和一个语义 Score。历史 Experiment 不会因为新建规则自动补分。

更完整的 `assistant-agent-real-system-v1` Dataset 覆盖无工具克制、必要澄清、天气、日历读取、
本地文件、购物、网页搜索/抓取、图片理解、图片生成、多工具组合和日历写入确认边界。它使用项目级
`assistant-agent-task-quality-zh` Evaluator 生成 `agent.task_quality`：裁判同时消费用户请求、
案例验收标准、最终回答和实际 Tool Trace，检查任务是否真正完成、是否有工具依据以及是否违反约束。
该 criteria-aware 裁判取代只看最终文本的通用 Helpfulness，避免把成功生成的 artifact 误判为未生成，
或忽略等待确认与已执行之间的差异。裁判理由限制为 20 个汉字以内的中文。

运行完整真实系统 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --real-system \
  --allow-real-tools \
  --run-name my-real-system-eval
```

该 profile 使用当前 Registry 中实际配置成功的能力，但不自动执行未确认写操作，也不写入真实 Memory。
日历创建案例只验证 confirmation guard，不会制造无法清理的真实事件。

完整运行应满足：Dataset item 数量与 seed 一致，并且每个 item 都有六个确定性 Score。真实 Runtime
抛出的网络或协议异常会被评测边界转换为 `terminal_status=failed` 和
`provider_result_kinds=["error"]`，从而保留失败样本并让 Code Evaluator 正常评分。评估 Agent
是否真正完成任务时，应同时检查 `agent.strict_pass=true` 和 `agent.task_quality>=0.75`；
前者只证明硬性闭环，不能替代语义质量。

本机自托管实例当前使用独立的 `deepseek-judge` LLM Connection 作为默认裁判模型：

```text
adapter: anthropic
base URL: https://api.deepseek.com/anthropic
model: deepseek-v4-flash
```

选择 Anthropic-compatible API 是因为 Langfuse 3.221.1 的结构化输出探测与
OpenAI-compatible JSON mode 存在兼容限制；DeepSeek 官方 Anthropic API 支持裁判所需的
tool-based structured output。裁判仍可能偶发返回无法解析的结构化结果；缺失的 LLM Score
属于 evaluator 基础设施失败，不得记成 Agent 通过或失败，必要时应重试裁判。凭据只在
Langfuse UI 中配置，不进入仓库。若本机代理使用
`198.18.0.0/15` fake-IP DNS，需要在未跟踪的 `.data/langfuse/.env` 中为自托管实例设置：

```text
LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST=api.deepseek.com
```

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

只校验第一批真实 Dataset seed，不连接 Langfuse，也不调用 Provider：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --real-readonly --dry-run
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

第一次创建真实只读 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --real-readonly \
  --seed-only
```

真实运行会调用已配置的 Chat Provider 和 weather MCP，必须由 operator 显式确认：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --real-readonly \
  --allow-real-tools \
  --run-name my-first-real-readonly-eval
```

在运行前，需要在 Langfuse Evaluators 中部署最新 `agent_strict_pass.ts`，并让 Code Evaluator
rule 匹配 `assistant-agent-real-readonly-v1` Dataset；同时启用上述项目级中文 Helpfulness
Evaluator。Score 均由 Langfuse 异步生成，不由 Python runner 回写。

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
