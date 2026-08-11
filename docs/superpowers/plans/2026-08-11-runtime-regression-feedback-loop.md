# 真实对话回归闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立“日常对话 → Langfuse Live Score → 失败 observation 人工确认沉淀 → Langfuse Dataset Experiment 重跑”的单一闭环，并废止错误的 runtime issue → Git YAML 晋升路径。

**Architecture:** 日常评分继续由 Langfuse 原生 Live Observation Rule 完成；回归案例直接引用 Langfuse 中已有的 trace、observation 和失败 Score，并写入独立的 `assistant-agent-runtime-regressions` Dataset。Experiment 从 Dataset 读取原始用户请求，通过生产 `AgentGraphRuntime` 重放，输出结构化运行证据，再由与日常评分同源的 Langfuse Experiment Rule 重新评分。

**Tech Stack:** Python 3.12、Langfuse Python SDK 4.14、Langfuse 4.6 unstable evaluator/rule API、Pydantic、pytest。

## Global Constraints

- 默认中文文档与控制台语义；代码标识符保持英文。
- pytest 始终 `mock/local/offline`；真实 Provider 只在显式 `real` 模式与 allow gate 下运行。
- 不复制 Agent loop；Experiment 必须调用 `AgentGraphRuntime`。
- Dataset 写入必须显式指定一个已落库的失败 Score，并带独立写入门禁。
- 不修改或回滚工作区中现有 citation、workflow、gateway 等用户改动。
- 旧 `assistant-agent-release-review` Dataset 可保留为历史数据，但不再作为真实问题反馈闭环的数据源。

---

### Task 1: 删除错误的 Git Scenario 晋升路径

**Files:**
- Delete: `evals/release_review/runtime_promotion.py`
- Modify: `evals/release_review/contracts.py`
- Modify: `evals/release_review/cli.py`
- Delete: `tests/tdd/runtime-eval-feedback-loop/test_runtime_promotion.py`

**Interfaces:**
- Consumes: 当前 Release Review Scenario loader 与 CLI。
- Produces: Release Review 恢复为纯 Git-owned pre-release suite，不再声称接收日常 trace。

- [ ] **Step 1: 删除旧 promotion 测试，并确认 CLI/contract 中仍有旧符号引用**

Run:

```bash
rg -n "runtime_promotion|promote-runtime|RuntimeScenarioProvenance" evals tests/tdd/runtime-eval-feedback-loop
```

Expected: 命中旧 CLI、contract、module 与测试。

- [ ] **Step 2: 删除 module，移除 CLI action/参数/branch，并从 `ReleaseScenario` 移除 `provenance`**

实现后 `scripts/run_release_review.py --help` 不再暴露 `--list-runtime-candidates` 或 `--promote-runtime-candidate`。

- [ ] **Step 3: 运行 Release Review 既有临时测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment
```

Expected: PASS。

### Task 2: 从失败 Score 直接沉淀 Dataset item

**Files:**
- Create: `evals/runtime_regression/__init__.py`
- Create: `evals/runtime_regression/dataset.py`
- Create: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_dataset.py`

**Interfaces:**
- Consumes: `client.api.scores_v3.get_many_v3(...)`、`client.api.observations.get_many(...)`、`client.create_dataset_item(...)`。
- Produces: `promote_failed_score(client, *, score_id: str, reviewed_by: str) -> RuntimeRegressionPromotionResult`。

- [ ] **Step 1: 写 RED 测试**

测试必须证明：只有 `assistant_agent.quality.*`、`source=EVAL`、BOOLEAN `false` 且 subject 为 observation 的 Score 可晋升；函数从同 trace 的 root `agent.runtime` 提取未截断用户文本；Dataset item 使用稳定 ID、`source_trace_id`、`source_observation_id`，并记录同 trace 的全部失败 canonical Score。

- [ ] **Step 2: 运行 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_dataset.py
```

Expected: FAIL，原因是 module/function 尚不存在。

- [ ] **Step 3: 最小实现 Dataset promotion**

固定 Dataset：

```python
RUNTIME_REGRESSION_DATASET = "assistant-agent-runtime-regressions"
```

Dataset input 为 `{"request": <root user content>}`；expected output 为 `{"required_scores": {<failed score name>: True}}`；item source 指向原 trace 和触发失败的 observation。拒绝缺失内容、截断内容、非失败 Score 和非 observation Score。

- [ ] **Step 4: 运行 GREEN**

Run: 同 Step 2。

Expected: PASS。

### Task 3: 用生产 Runtime 重跑 Dataset

**Files:**
- Create: `evals/runtime_regression/experiment.py`
- Create: `evals/runtime_regression/cli.py`
- Create: `scripts/run_runtime_regressions.py`
- Create: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`

**Interfaces:**
- Consumes: `RUNTIME_REGRESSION_DATASET`、`AgentGraphRuntime.run_state(UserRequest)`、`ReleaseRunEvidence.from_state(...)`。
- Produces: `run_runtime_regression_experiment(client, settings) -> RuntimeRegressionExperimentResult`；CLI `--promote-score`、`--run`、`--inspect`。

- [ ] **Step 1: 写 RED 测试**

测试 Dataset item schema、隔离的 `user_id/session_id`、生产 runtime factory 调用、结构化 evidence 输出、关闭 runtime，以及 `run_experiment` 的固定 Dataset/name/metadata。

- [ ] **Step 2: 运行 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py
```

Expected: FAIL，原因是 runner 尚不存在。

- [ ] **Step 3: 实现 Experiment 与 CLI gate**

`--promote-score` 要求 `--score-id --reviewed-by --allow-dataset-write`；`--run` 要求 `--allow-real-provider` 且 `MULTIMODAL_AGENT_PROVIDER_MODE=real`。Experiment 输出直接使用 `ReleaseRunEvidence.model_dump(mode="json")`，不创建第二套 Agent 逻辑。

- [ ] **Step 4: 运行 GREEN 与 CLI help**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_regressions.py --help
```

Expected: PASS，help 显示三种 action 与写入/真实 Provider gate。

### Task 4: 让同一 evaluator family 同时评分 Live 与 Experiment

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/online_evaluators.py`
- Modify: `tests/tdd/runtime_audit/test_runtime_audit.py`

**Interfaces:**
- Consumes: `assistant-agent-runtime-regressions` Dataset Run。
- Produces: 5 条 canonical Live Observation Rule，加 `response_quality.experiment` 与 `grounding.experiment` 两条 Experiment Rule；均引用同一 canonical evaluator family。

- [ ] **Step 1: 写 RED 测试**

断言 7 条 Rule、Experiment target、Dataset filter、input/output mapping，并断言 evaluator prompt/model/output definition 发生漂移时创建新 version、完全一致时幂等跳过。

- [ ] **Step 2: 运行 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime_audit/test_runtime_audit.py -k native_online_evaluator
```

Expected: FAIL，原因是当前仅创建 5 条 Live Rule，且不 reconcile evaluator version。

- [ ] **Step 3: 实现共享 evaluator 与两类 Rule**

Live Rule 继续使用 canonical 名；Experiment Rule 使用 `.experiment` 后缀，filter 只允许 `assistant-agent-runtime-regressions`。`response_quality`/`grounding` prompt 同时解释 Live observation 与 `ReleaseRunEvidence` Experiment output。

- [ ] **Step 4: 运行 GREEN**

Run: 同 Step 2。

Expected: PASS。

### Task 5: Langfuse v4 日审计读取兼容

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/langfuse_source.py`
- Modify: `tests/tdd/runtime_audit/test_runtime_audit.py`

**Interfaces:**
- Consumes: Observations API v2、Scores API v3。
- Produces: 与现有 collector 兼容的 `list_traces(window_start, window_end) -> list[LangfuseTraceSnapshot]`，不再调用 v4 events-only 已移除的 `api.trace.list/get`。

- [ ] **Step 1: 写 RED 测试**

用分页 observation fixture 重建按 `traceId` 聚合的 snapshot，验证 root `agent.runtime`、children、Score、时间窗口和 trace URL；fake client 不提供 `api.trace`。

- [ ] **Step 2: 运行 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime_audit/test_runtime_audit.py -k langfuse
```

Expected: FAIL，原因是当前仍访问 legacy trace API。

- [ ] **Step 3: 实现 v2 聚合适配**

只读取 `assistant.turn` 的 observation 数据，按 trace ID 聚合并解析 v2 raw-string IO；继续使用 Score v3，保持现有 redaction 与 snapshot schema，不回填不存在的 trace input/output。

- [ ] **Step 4: 运行 GREEN**

Run: 同 Step 2。

Expected: PASS。

### Task 6: 权威文档与真实端到端验收

**Files:**
- Modify: `evals/README.md`
- Modify: `docs/observability-harness.md`
- Modify: `scripts/README.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Consumes: Tasks 1–5 的最终 CLI、Dataset 与 Rule 名称。
- Produces: 唯一书面流程“日常对话 → Score → promote failed score → Dataset → Experiment rerun”。

- [ ] **Step 1: 更新 owner authority**

删除 runtime issue → Git Scenario promotion 文案；明确旧 Release Review Dataset 仅为历史 pre-release suite，不属于反馈闭环；加入 `scripts/run_runtime_regressions.py` 命令、gate、Dataset 名与 Score 完整性说明。

- [ ] **Step 2: 运行最小离线验证与文档 authority 检查**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop tests/tdd/runtime_audit
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

Expected: PASS。

- [ ] **Step 3: 用本轮真实失败 Score 写入 Dataset**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_regressions.py \
  --promote-score --score-id b14b7e82-cc96-5a4e-95da-2de85b7a300a \
  --reviewed-by codex --allow-dataset-write
```

Expected: 创建一个链接 source trace/observation 的 ACTIVE Dataset item，失败维度为 `assistant_agent.quality.grounding`。

- [ ] **Step 4: 配置 Experiment Rule 并真实重跑**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
  configure-evaluators --model-provider qwen-judge --model qwen-flash \
  --apply --allow-online-judge
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_regressions.py \
  --run --run-name first-real-feedback-loop --allow-real-provider
```

Expected: Langfuse 生成一个 Dataset Run，重放原请求，并落库 `response_quality` 与 `grounding` Experiment Score；最终报告真实 Provider 调用范围与结果。

