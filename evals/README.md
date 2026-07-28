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

本地 `eval_manifest_v1.json` 是评测结构索引，不是第二套结果账本。它集中定义：

| concept | responsibility |
| --- | --- |
| Case | 一个稳定 `case_id` 对应的用户场景 |
| Capability | 与 Provider 和环境无关的被测行为 |
| Profile | Chat、Tool、fixture 和副作用的执行边界 |
| Suite | 某个 Profile 下可复用的 Case 选择 |
| Experiment | Suite × Profile × 代码版本的一次 Langfuse 运行 |

当前 Profile 为 `scripted_mock`、`real_readonly` 和 `real_system`；默认 Suite 分别为
`infrastructure_baseline`、`readonly_smoke` 和 `system_full`。`failure_recovery` 是
`real_readonly` 下只选择 `tool_failure_recovery` capability 的窄 Suite。Case metadata 必须分别
记录稳定 `capability` 和 `profile`，不得再用 `real_*` capability 表达执行环境。

`agent_closed_loop_v1.seed.json` 只用于第一次创建 Dataset 或 operator 显式重置 seed。普通 Experiment
直接读取 Langfuse Dataset，不会用本地文件覆盖 UI 修改。`agent_strict_pass.ts` 是需要部署到
Langfuse Evaluators 页面的 Code Evaluator 源码。

`personal_assistant_daily.catalog.json` 保留原有 20/10/5 个人助理案例语料，作为后续导入 Langfuse
Dataset 的候选 catalog；当前 Experiment 不会自动执行它，也不存在第二套本地评分 runner。

当前 Experiment 默认使用 scripted mock，只验证 Langfuse 闭环基础设施，不代表真实模型泛化能力。
`agent_real_readonly_v1.seed.json` 是第一批真实模型 Dataset seed，包含 2 个 no-tool 和 3 个真实天气
案例，以及 1 个受控天气超时恢复案例。恢复案例仍使用真实 Chat Provider，但只在该 Dataset item
内通过生产 `WeatherAdapter` 接口注入确定性的 `provider_timeout`，不会调用真实天气服务；它必须
产生完整 `tool.failed` 证据并由 Agent 诚实降级。整个 Dataset 仍通过同一个 Langfuse
Dataset/Experiment 入口注入 real Runtime，不恢复独立本地 case runner。真实 profile 强制关闭
Mem0、持久化会话、checkpointer 和 durable task，并要求 `--allow-real-tools`，避免把 scripted
日历写入 Dataset 误用于真实环境。

`agent_strict_pass.ts` 同时支持 scripted baseline 和 real-readonly Dataset，并为每个 Dataset item
产生两个 Langfuse 原生确定性 Score：`agent.runtime_trace_pass` 检查运行终态和 Trace 完整性，
`agent.tool_mechanical_pass` 只检查已发生工具调用的暴露、Validator、工具 Trace 和结构化执行终态；
`tool.finished` 与携带结构化错误的 `tool.failed` 都能证明机械调用链闭合，外部 Provider 或业务结果
是否命中不在该分数重复判断；
工具暴露证据按每次 `tool.started` 之前最近一次 `context.build.finished` 的
`selected_tool_names` 记录，最终 answer-only / FINALIZE 轮次的空 catalog 不会覆盖前面执行轮次的
真实暴露事实；`available_tools` 仅汇总整次 run 曾暴露的工具，供诊断和兼容检查。
没有工具调用且没有失败链路时也记为通过。它不比较 Dataset 的必需/禁止工具，也不判断是否应该调用、
工具选择、参数或结果语义，这些由 `agent.tool_semantic_pass` 负责。底层细分检查保留在
Score metadata 中供失败诊断，不再各自占据 UI。最终回答质量由 Langfuse 原生
LLM-as-a-Judge Evaluator 单独评估。

更完整的 `assistant-agent-real-system-v1` Dataset 覆盖无工具克制、必要澄清、天气、日历读取、
本地文件、购物、网页搜索/抓取、图片理解、图片生成、多工具组合和日历写入。它使用项目级
`assistant-agent-tool-semantic-pass-zh` 和 `assistant-agent-answer-semantic-pass-zh` 两个裁判：
`agent.tool_semantic_pass` 判断工具选择、参数和结果在用户语境中是否正确，
`agent.answer_semantic_pass` 判断最终回答是否忠于工具证据并满足验收标准。这样可以区分“工具机械执行
成功但语义错误”和“工具语义正确但最终回答错误”。裁判理由不限制为固定字数，应说明判定、关键
Trace 证据、失败原因和可操作的改进方向，但不复述无关 Trace 内容。

检索类 Tool 的 `outcome=empty` 表示 Provider 正常完成但没有命中，机械分应通过；语义裁判应继续检查
调用是否必要、URL/查询词/时间窗是否合理，以及 Agent 是否如实使用空结果。合理查询得到空结果可以
通过 `agent.tool_semantic_pass`，错误 URL 或会排除目标结果的筛选参数不能仅因调用已完成而通过。
`outcome=failed` 也不反向破坏机械链路分，但其外部依赖状态、Agent 恢复行为和最终回答必须由语义
证据分别判断。

四个 Score 的字段按 Langfuse 原生语义分工：项目级 Score Config 的 `description` 固定说明
“该分数评估什么”，单次 Score 的 `value` 保存布尔结论，`comment` 只说明本次通过或失败的原因。
确定性 Score 的细分证据继续保存在 `metadata.checks`。Langfuse 的单条 Score 记录本身没有
`description` 字段，不应把固定用途重复拼入每次 `comment`。

这两个 LLM-as-a-Judge 规则同时匹配 `assistant-agent-closed-loop-v1`、
`assistant-agent-real-readonly-v1` 和 `assistant-agent-real-system-v1`，sampling 为 `100%`。
因此所有新的 Agent Experiment 统一产生四个分层 Score；scripted mock 的两个语义 Score 只验证
裁判闭环，不代表真实模型泛化能力。旧的 `agent.answer_helpfulness` 规则已停用并归档。历史
Experiment 不会因规则变更自动补分或改分。

运行完整真实系统 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_system \
  --allow-real-tools \
  --run-name my-real-system-eval
```

该 profile 使用当前 Registry 中实际配置成功的能力，不写入真实 Memory。写工具被本轮 Tool Catalog
暴露并通过 Validator 后会直接执行。日历读取和创建固定使用
`.data/evals/langfuse/calendar.sqlite3`，按 `user_id` 隔离，不调用 Google Calendar MCP；可用
`--local-calendar-path` 指定其他未跟踪数据库。Trace 继续承担工具执行审计，SQLite 只保存可检索的
日历业务状态。

完整运行应满足：Dataset item 数量与 seed 一致，并且每个 item 都有四个分层 Score。真实 Runtime
抛出的网络或协议异常会被评测边界转换为 `terminal_status=failed` 和
`provider_result_kinds=["error"]`，从而保留失败样本并让 Code Evaluator 正常评分。评估 Agent
是否真正完成任务时，四个 Score 应同时为 `true`：
`agent.runtime_trace_pass`、`agent.tool_mechanical_pass`、
`agent.tool_semantic_pass` 和 `agent.answer_semantic_pass`。

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
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --dry-run
```

精确校验单个 Case：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --case-id agent_real_v1_weather_timeout_running_recovery \
  --dry-run
```

按稳定 capability 或命名 Suite 选择：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --capability weather_advice \
  --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --suite failure_recovery \
  --dry-run
```

同类选择器重复传入时取并集；`--case-id`、`--capability` 和 Suite 之间取交集。Suite 已固定
Profile，因此不能与不兼容的 `--profile` 组合。

### Langfuse 运行

第一次显式创建或重置 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --seed-only
```

显式 seed 会 upsert 当前 seed item，并删除带旧 `seed_hash`、但已不在当前 seed 中的历史托管
item；这样删除 capability 后不会继续执行旧案例。没有 `seed_hash` 的 UI 手工案例不会被删除。

运行 Experiment：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --run-name my-agent-eval
```

只重跑某次 Experiment 中明确未通过的 Dataset item：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_system \
  --allow-real-tools \
  --rerun-failed-from my-real-system-eval \
  --run-name my-real-system-eval-retry
```

`--rerun-failed-from` 按同一 Dataset 中指定 run 的四个原生 Score 选择用例；任一 Score
最新值明确为 `false` 时重跑该 item。异步裁判尚未产生的缺失 Score 属于评测基础设施状态，
不会被误判为 Agent 失败。若没有明确失败项，命令直接退出，不创建新的 Experiment。显式传入
`--rerun-failed-from none` 等同于不启用失败筛选，执行全量 Dataset。

历史 run 可能引用已从当前 Dataset seed 删除的旧 item，尤其是在同一命令使用
`--seed-dataset` 时。重跑以同步后的当前 Dataset 为执行权威：旧 item 不恢复、不执行，并在输出的
`skipped_unavailable_item_ids` 中明确列出；仍存在的失败 item 继续进入新 Experiment。

第一次创建真实只读 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --seed-only
```

真实运行会调用已配置的 Chat Provider；其中 3 个天气成功案例还会调用 weather MCP，受控失败案例
不会调用真实天气服务。整个 Experiment 必须由 operator 显式确认：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --allow-real-tools \
  --run-name my-first-real-readonly-eval
```

只运行受控天气失败恢复案例，不要求真实 weather MCP：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --suite failure_recovery \
  --allow-real-tools \
  --run-name weather-failure-recovery
```

`--real-readonly` 和 `--real-system` 暂时保留为对应 `--profile` 的兼容别名。`--case-id` 和
`--capability` 可以重复；它们不能与 `--rerun-failed-from` 混用，避免不明确的二次筛选。

在运行前，需要在 Langfuse Evaluators 中部署最新 `agent_strict_pass.ts`，并让 Code Evaluator
rule 匹配 `assistant-agent-real-readonly-v1` Dataset；同时启用项目级
`assistant-agent-tool-semantic-pass-zh` 和 `assistant-agent-answer-semantic-pass-zh`。
Score 均由 Langfuse 异步生成，不由 Python runner 回写。

命令默认从未跟踪的 `.env` 加载 Langfuse 凭据和 host。显式 Experiment 对 Dataset、Runtime OTLP trace
和 evaluator 闭环采用 fail-fast；普通生产观测仍保持 fail-open。

## 统一安全要求

- 不提交 API key、token、真实 `.env`、Provider 原始响应或真实用户数据；
- system eval 使用唯一测试身份和只读场景优先；
- 写入型场景必须具备独立 namespace、幂等键和可验证 cleanup；
- 缺少配置、认证失败、超时、限流或外部服务异常必须明确失败或 blocked；
- system eval artifact 写入 `.data/evals/system/`；
- case eval 的 Dataset、Experiment 和 Score 以 Langfuse 为权威；
- 本轮调用真实 Provider 时，任务总结必须报告调用范围和结果。
