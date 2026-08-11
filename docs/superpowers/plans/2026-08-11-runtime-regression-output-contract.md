# Runtime Regression 输出契约修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Langfuse Runtime Regression 的主 output 与 Dataset 中的普通 Assistant baseline 结构兼容，同时保留独立的执行证据和 baseline-aware 回归评分。

**Architecture:** Runtime Regression task 只返回 canonical Assistant output；原有 `ReleaseRunEvidence` 写入当前 `experiment-item-task` 的专用 metadata 字段，供 Experiment grounding evaluator 映射。新增独立的 regression-improvement evaluator，把 Dataset `expected_output` 明确解释为原始失败 baseline，而不是 golden answer。

**Tech Stack:** Python 3.12、Pydantic、Langfuse Python SDK/Otel、pytest。

## Global Constraints

- 不修改 Agent Runtime 主循环、Provider 或 Tool 治理链。
- pytest 仅使用 mock/offline；真实验证必须显式使用现有 Runtime Regression CLI。
- 固定 Dataset 仍为 `assistant-agent-runtime-regressions`，现有 UI Item 不迁移、不重写。
- 主 output 固定包含 `role/content/chars/truncated/terminal_status`。
- 原始 `expected_output` 是失败 baseline，不是要求新输出逐字模仿的正确答案。

---

### Task 1: Canonical output 与 evidence 分离

**Files:**
- Modify: `evals/runtime_regression/experiment.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`

**Interfaces:**
- Consumes: `AgentState`、`ReleaseRunEvidence.from_state(state, events)`、Langfuse client `update_current_span(metadata=...)`。
- Produces: task output `dict[str, object]`，以及 metadata key `runtime_regression_evidence`。

- [ ] **Step 1: 写 RED 测试**

测试运行一次 mock item 后，断言主 output 只有 canonical Assistant 字段，回答位于 `content`；同时断言 `runtime_regression_evidence` 已写入当前 span metadata，包含 calls/final_state/infrastructure_error。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py::test_runtime_regression_experiment_replays_active_item_through_runtime`

Expected: FAIL，因为当前 task output 仍是 `ReleaseRunEvidence`。

- [ ] **Step 3: 实现最小投影**

增加纯函数生成 canonical Assistant output；task 内继续构造 `ReleaseRunEvidence`，将其 JSON-safe dict 写入当前 task span metadata，再返回 canonical output。缺少 response 时仍返回空 content 与真实 terminal status，交给评分判 false，不伪装基础设施成功。

- [ ] **Step 4: 运行定向测试确认 GREEN**

执行 Task 1 的测试文件，确认结构与 metadata evidence 均通过。

### Task 2: baseline-aware Experiment Evaluators

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/online_evaluators.py`
- Modify: `evals/runtime_regression/experiment.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_runtime_audit.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`

**Interfaces:**
- Consumes: `EvaluationRuleMappingSource.EXPECTED_OUTPUT`、`METADATA`、`EXPERIMENT_ITEM_METADATA`。
- Produces: `assistant_agent.quality.grounding.experiment` 的 evidence mapping，以及新 Score `assistant_agent.quality.regression_improvement.experiment`。

- [ ] **Step 1: 写 RED 测试**

断言 grounding Experiment Rule 使用独立 evaluator，并把 `runtime_regression_evidence` metadata 映射为 `evidence`；断言 regression-improvement Rule 映射 input/output/expected_output/item metadata；断言 Runtime Regression 等待三项 canonical Score。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop`

Expected: FAIL，因为当前只有两项 Experiment Score，且所有 rule 只有 input/output mapping。

- [ ] **Step 3: 实现 evaluator 与 rule mapping**

为 `_RuleSpec` 增加显式 mappings；保留 live evaluator 不变。Experiment response-quality 读取 canonical output；Experiment grounding 读取 output 和 evidence；regression-improvement 把 expected output 称为 baseline，并结合 item metadata 判断原故障是否消失。

- [ ] **Step 4: 更新 Score 完整性门禁并确认 GREEN**

把新 Score 加入 Runtime Regression 的 required score 集合，运行整个 feature TDD 目录。

### Task 3: 权威文档与验证

**Files:**
- Modify: `evals/README.md`
- Modify: `docs/observability-harness.md`（仅在 owner contract 需要时）

**Interfaces:**
- Produces: UI 可理解的 baseline/current output 契约与三项 Runtime Regression Score 说明。

- [ ] **Step 1: 同步 eval 权威**

记录 canonical output、metadata evidence、baseline 非 golden 的语义，以及三项 Score 和 fail-closed 行为。

- [ ] **Step 2: 运行离线验证**

Run feature TDD、`scripts/check_documentation_authority.py --repo-root .`、`python -m compileall -q src/assistant_agent evals` 与 `git diff --check`。

- [ ] **Step 3: 真实 Experiment 验证**

用现有 CLI 直接运行一个 active item，不启动 8089。远端读取 Experiment item，确认解析后的 output keys 与 baseline 一致、evidence metadata 存在、三项 Score 落库、完整 Runtime trace 层级仍通过。

- [ ] **Step 4: 审查并提交**

只暂存本任务文件/hunk，保留工作区其他用户改动；提交后在提交快照的临时 worktree 中复验定向测试。
