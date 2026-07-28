# Agent Eval 体系

`evals/` 只保存真实系统能力验证或 Agent 行为评分，不属于 pytest。

```text
evals/
  system/                  # 真实能力连通性；本地 runner/artifact 是权威
  cases/langfuse/          # Agent 行为；Langfuse Dataset/Experiment/Score 是权威
```

## System eval

System eval 回答“真实 Provider、Tool、Context 或 Memory 是否连通并经过治理链路”。所有真实运行必须
显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=real`，完整配置所需 Provider，并由 operator 传入对应
确认参数；不能检测到 key 后自动运行，也不能从 real 静默回退 mock。

Tool 连通性：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id weather_beijing_today
```

结果写入 `.data/evals/system/tools/<run>/`。

Context 捕获：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_context_eval.py \
  --allow-unredacted-context
```

仅允许合成输入，结果写入 `.data/evals/system/context/<run>/`，不得提交。

Memory 当前没有完整 delete/reset 公共契约，因此不提供会写入真实 Mem0 的自动 runner；详见
`evals/system/memory/README.md`。

## Langfuse case eval

```text
Langfuse Dataset
  -> Experiment
  -> AgentGraphRuntime
  -> AgentExperimentOutput
  -> Code Evaluator + LLM-as-a-Judge
  -> Score
```

Langfuse 是 Dataset、Experiment、Evaluator 和 Score 的运行时权威。本地 JSON 只负责受版本控制的
结构索引与显式 Dataset 同步，不是第二套结果账本。

### 核心模型

| concept | responsibility |
| --- | --- |
| Case | 稳定 `case_id` 对应的用户场景 |
| Capability | 与 Provider/环境无关的被测行为 |
| Dataset | 稳定案例集合 |
| Suite | 从 Dataset 选择一组 Case |
| Profile | Chat、Tool、fixture 和副作用执行边界 |
| Experiment | Dataset × Suite × Profile × 代码版本的一次运行 |

活动 Dataset 只有两个：

| key | Langfuse Dataset | purpose |
| --- | --- | --- |
| `infrastructure` | `assistant-agent-infrastructure-v1` | scripted Runtime、Trace、Evaluator 闭环 |
| `behavior` | `assistant-agent-behavior-v2` | 全部真实 Agent 行为案例 |

旧 `assistant-agent-closed-loop-v1`、`assistant-agent-real-readonly-v1` 和
`assistant-agent-real-system-v1` 只保留历史 Experiment，不再由本地 seed 管理，也不会自动删除。

结构权威入口：

- `eval_manifest_v2.json`：Dataset、Profile、Suite、Capability 和归档 Dataset；
- `agent_infrastructure_v1.seed.json`：infrastructure seed；
- `agent_behavior_v2.seed.json`：统一 behavior seed；
- `evaluators/evaluator_manifest_v1.json`：Evaluator、源码、Score 和 Langfuse rule 对应关系。

Case metadata 记录 `capability`、`compatible_profiles`、依赖、工具和副作用事实。Profile 不拥有
Dataset；Suite 选择 Dataset，Profile 只决定怎样运行所选 Case。

### 代码职责

```text
contracts.py          # Dataset/Case/ExperimentOutput schema
dataset_sync.py       # 本地 managed items -> Langfuse Dataset
manifest.py           # Dataset/Profile/Suite/Capability 与选择
runtime_profiles.py   # scripted_mock / real_readonly / real_system
evidence.py           # Runtime Trace -> evaluator evidence
experiment.py         # 薄 Experiment task/orchestration
evaluators/           # Code Evaluator 与版本清单
```

### Score

每个 Experiment item 应获得四个分层 Score：

- `agent.runtime_trace_pass`：Runtime 终态与 Trace 闭环；
- `agent.tool_mechanical_pass`：工具暴露、Validator、执行与终态；
- `agent.tool_semantic_pass`：工具选择、参数、结果使用和状态语义；
- `agent.answer_semantic_pass`：最终回答忠于证据并满足任务。

`tool.finished` 和携带结构化错误的 `tool.failed` 都可以证明机械链路闭合；业务结果成功、失败、empty
及 Agent 恢复行为由语义 Score 判断。Judge 超时、解析失败或缺失 Score 属于评测基础设施状态，不得
伪装成 Agent 通过或失败。

Code Evaluator 源码在 `evaluators/agent_strict_pass.ts`。两个 LLM Judge
`assistant-agent-tool-semantic-pass-zh` 和 `assistant-agent-answer-semantic-pass-zh` 在 Langfuse
UI 部署，版本对应关系记录在 Evaluator manifest；凭据不进入仓库。使用 DeepSeek Anthropic
connection 时，Model parameters 必须设置
`providerOptions.anthropic.thinking.type=disabled`，避免只返回 reasoning block 而缺少 Langfuse
所需的最终结构化对象；该要求同时记录在 Evaluator manifest。

## 命令

### 离线检查

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

检查默认 infrastructure 选择：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --inspect
```

检查 behavior Suite：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --inspect

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --suite failure_recovery \
  --inspect
```

`--case-id` 和 `--capability` 可重复；同类值取并集，不同选择维度取交集：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_system \
  --capability shopping_list_search \
  --inspect
```

### 同步 Dataset

同步完整 managed Dataset 后退出：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --sync-dataset-only
```

同步会 upsert 本地 managed items，删除同一 Dataset 中带旧 `seed_hash` 且已不在当前 seed 的 managed
items，但保留没有 `seed_hash` 的 Langfuse UI 手工案例。它不运行 Agent、Provider、Evaluator。

`--seed-only` 是 `--sync-dataset-only` 的兼容别名；`--seed-dataset` 是 `--sync-dataset` 的兼容
别名；`--dry-run` 是 `--inspect` 的兼容别名。

### 运行 Experiment

真实 profile 需要 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、完整配置和显式
`--allow-real-tools`：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --allow-real-tools \
  --run-name behavior-readonly-smoke
```

只运行受控失败恢复，不调用真实 weather：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --suite failure_recovery \
  --allow-real-tools \
  --run-name weather-failure-recovery
```

完整 system Suite 可能执行隔离写操作，日历固定使用
`.data/evals/langfuse/calendar.sqlite3`：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_system \
  --allow-real-tools \
  --allow-writes \
  --run-name behavior-system-full
```

运行前同步统一 behavior Dataset：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_readonly \
  --sync-dataset \
  --allow-real-tools \
  --run-name behavior-readonly-after-sync
```

只重跑某个 run 中最新四个 Score 任一明确为 `false` 的当前 Dataset items：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --profile real_system \
  --allow-real-tools \
  --allow-writes \
  --rerun-failed-from behavior-system-full \
  --run-name behavior-system-retry
```

缺失异步 Score 不视为明确失败。历史 run 中已不在当前 Dataset 的 item 会列入
`skipped_unavailable_item_ids`，不会恢复执行。

## 统一安全要求

- 不提交 API key、token、真实 `.env`、Provider 原始响应或真实用户数据；
- pytest 只使用 mock/local/offline；
- system eval 真实调用必须显式授权；
- Langfuse behavior 写场景只使用隔离、可复位和可观察状态；
- case eval 的 Dataset、Experiment 和 Score 以 Langfuse 为权威；
- 本轮调用真实 Provider 时，最终报告必须说明范围和结果。
