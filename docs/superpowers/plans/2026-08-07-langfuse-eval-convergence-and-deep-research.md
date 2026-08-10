# Langfuse Eval 收敛与深度研究 Mission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以 Langfuse 4.6 原生 Evaluator family 统一日常 Trace 与 Experiment 的语义评分，并新增三个验证深度研究长流程能力的 Agent eval Mission。

**Architecture:** `assistant_agent.quality.response_quality` 与 `assistant_agent.quality.grounding` 各保留一个版本化 Evaluator family，并分别绑定 UI 可独立启停的 Live Observation Rule 与 Experiment Rule。Experiment runner 只在本地计算 Git/Environment 拥有的确定性 `task_conformance`，随后通过 Scores v3 按 `experiment-item-task` observation 回查两个原生 Judge Score；旧 `ProviderLLMJudge` 不再进入正式 run。深度研究案例通过受控 `WorkflowService + InMemoryWorkflowStore + DeepResearchWorkflowDefinition` 运行真实 `AgentGraphRuntime -> workflow_submit` 链路，并以 Mission state Rule 验证创建的 Workflow。

**Tech Stack:** Python 3.12、Langfuse Python SDK 4.x / Server 4.6、Pydantic、AgentGraphRuntime、durable workflow、pytest。

## Global Constraints

- 默认 pytest 与所有 TDD 保持 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider 或 Langfuse。
- 正式 Agent Experiment 仍必须显式使用 real Provider；Judge 的实际启停和采样只由 Langfuse Evaluation Rule 管理。
- 不覆盖工作区已有 `evals/agent/langfuse_backend.py` Observations v2 迁移及 `tests/tdd/langfuse-v4-migration/`。
- 不把 evaluator 启停开关加入 Agent Runtime；已有 UI 管理的 `enabled`、`sampling` 不得被普通 reconcile 重置。
- Git 继续拥有 Task、Environment、确定性 objective Rule 和人工校准标签；Langfuse 拥有 Evaluator、Rule、Experiment、Trace 与 Score。

---

### Task 1: 统一原生 Evaluator 与 Live/Experiment Rule

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/online_evaluators.py`
- Create: `tests/tdd/eval-convergence/test_native_evaluator_rules.py`

**Interfaces:**
- Produces: `configure_native_online_evaluators()` 为 response/grounding 创建 `.live` 与 `.experiment` 两条 Rule，其余 evaluator 只创建 `.live` Rule。
- Preserves: 已存在 Rule 的 `enabled`、`sampling` 运维状态。

- [ ] **Step 1: 写 RED 测试，证明现实现有 Rule reconcile 会覆盖 UI 状态且缺少 Experiment Rule。**
- [ ] **Step 2: 显式运行 `tests/tdd/eval-convergence/test_native_evaluator_rules.py`，确认按预期失败。**
- [ ] **Step 3: 将 evaluator spec 与 rule spec 分离，使用稳定 `.live` / `.experiment` Rule name；更新只写 evaluator reference、target、filter、mapping，不写 `enabled/sampling`。**
- [ ] **Step 4: 运行同一测试确认 GREEN，并保留 legacy Rule rename 兼容。**

### Task 2: 正式 Experiment 移除旧 Provider Judge 主路径

**Files:**
- Modify: `evals/agent/grading.py`
- Modify: `evals/agent/langfuse_backend.py`
- Modify: `evals/agent/cli.py`
- Modify: `evals/agent/contracts.py`
- Create: `tests/tdd/eval-convergence/test_native_experiment_scoring.py`

**Interfaces:**
- Produces: `grade_task_conformance(task, evidence) -> DimensionResult`，只执行 Environment outcome 与 Mission objective Rule。
- Produces: `run_tasks(..., config, ...)` 的本地 evaluator 只返回 `assistant_agent.quality.task_conformance`。
- Produces: `verify_persisted_dimension_scores(...) -> list[dict[str, bool]]` 按 task observation ID 等待并返回三个 canonical Score。

- [ ] **Step 1: 写 RED 测试，要求 `run_tasks` 不接受/调用 `LLMJudge`，且内存结果只含确定性 Score。**
- [ ] **Step 2: 写 RED 测试，要求 Scores v3 查询使用 `observation_id`，并把原生 Judge Score 返回给 CLI。**
- [ ] **Step 3: 运行定向测试并确认失败原因来自旧 `grade_task(..., judge)` 与 trace-wide Score 查询。**
- [ ] **Step 4: 提取确定性 conformance grader；删除正式 run 的 Judge 构造、timeout/retry 参数和旧三维内存检查。**
- [ ] **Step 5: 将持久化回查改为 observation-scoped，严格验证 BOOLEAN、subject 和三个 Score，并返回结果。**
- [ ] **Step 6: 运行定向测试确认 GREEN。旧 Judge 文件仅作为迁移兼容留存，不再被 CLI/run 导入。**

### Task 3: 将校准职责迁到 Langfuse 原生 Evaluator

**Files:**
- Modify: `evals/agent/calibration.py`
- Modify: `evals/agent/cli.py`
- Modify: `evals/agent/langfuse_backend.py`
- Create: `tests/tdd/eval-convergence/test_native_calibration.py`

**Interfaces:**
- Produces: calibration fixture 作为受控 Experiment item 执行，输出已有 `RunEvidence`；由 Experiment Rule 产生 grounding/response_quality，Git Rule 产生 task_conformance。
- Consumes: 现有 calibration v3 人工标签作为迁移兼容；不直接调用 Chat Provider Judge。

- [ ] **Step 1: 写 RED 测试，要求 calibration runner 不构造 `ProviderLLMJudge`，并比较持久化原生 Score 与人工标签。**
- [ ] **Step 2: 运行测试确认旧直接 Judge 路径失败。**
- [ ] **Step 3: 实现受控 calibration Experiment 和结果比对；Evaluator Rule 被 UI 暂停时因缺 Score 返回基础设施错误。**
- [ ] **Step 4: 运行测试确认 GREEN，并保留 `--allow-real-provider` 作为真实 Judge 成本的 operator 确认门禁。**

### Task 4: 新增深度研究受控 Environment

**Files:**
- Create: `evals/agent/deep_research_support.py`
- Create: `tests/tdd/eval-convergence/test_deep_research_environment.py`

**Interfaces:**
- Produces: `DeepResearchMissionEnvironment` 通过完整受控目录暴露真实 governed `workflow_submit`。
- Produces: final state 投影 workflow type、submission、plan stages 和持久事件；objective Rule 不依赖 Agent 自述。

- [ ] **Step 1: 写 RED 测试，要求 Environment validate 通过且 `workflow_submit` 必须成功。**
- [ ] **Step 2: 写 RED 测试，使用 scripted ChatAdapter 证明真实 Runtime 调用后可从 store 投影 Workflow state。**
- [ ] **Step 3: 实现 in-memory Workflow service、registry replacement、runtime override、final state reader 与 Mission objective assertions。**
- [ ] **Step 4: 运行定向测试确认 GREEN。**

### Task 5: 新增三个深度研究 Mission

**Files:**
- Create: `evals/agent/missions/deep_research_autonomous_admission/{__init__.py,task.json,environment.py,grader.py,calibration.json}`
- Create: `evals/agent/missions/deep_research_constraint_grounding/{__init__.py,task.json,environment.py,grader.py,calibration.json}`
- Create: `evals/agent/missions/deep_research_evidence_plan/{__init__.py,task.json,environment.py,grader.py,calibration.json}`
- Modify: `evals/agent/suites.json`

**Interfaces:**
- Mission 1: 验证 LLM 自主选择 `deep_research` Workflow，而非 Runtime 关键词路由。
- Mission 2: 验证用户研究范围、时间边界和交付物进入 Workflow submission/state。
- Mission 3: 验证多来源、冲突证据、引用与验证要求形成标准七阶段 Deep Research plan。

- [ ] **Step 1: 为三个 Mission 编写自然用户请求、唯一 capability 和受控 Environment 子类。**
- [ ] **Step 2: 为每个 Mission 提供正反人工 calibration Evidence；不把 oracle 或 rubric 写入 Dataset metadata。**
- [ ] **Step 3: 将三个 Mission 加入独立 `deep_research` suite。**
- [ ] **Step 4: 对三个 Mission 执行 `--inspect`，确认 Environment、Mission Rule 和完整目录契约有效。**

### Task 6: 同步权威文档与迁移说明

**Files:**
- Modify: `evals/README.md`
- Modify: `docs/observability-harness.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `.codex/skills/langfuse-eval-engineering/references/grader-audit.md`

**Interfaces:**
- Produces: 文档明确 Evaluator family / Live Rule / Experiment Rule / Git Rule 的所有权，以及 Langfuse 4.6 UI 启停语义。

- [ ] **Step 1: 删除“正式 Experiment 使用独立 ProviderLLMJudge”的当前态描述，改为原生 evaluator Score 回查。**
- [ ] **Step 2: 说明 UI 拥有 Rule 的 enabled/sampling，仓库 reconcile 不覆盖运维状态。**
- [ ] **Step 3: 登记三个 Deep Research Mission、运行命令和真实调用门禁。**
- [ ] **Step 4: 将仍适用于 3.224.2 的内容明确标为迁移历史，避免与 4.6 当前态混用。**

### Task 7: 验证、审查与提交

**Files:**
- Verify: all task-related source, eval, TDD and docs files.

**Interfaces:**
- Produces: 可审查 commit；不包含现有 `.run`、4.6 部署计划/spec 和其他用户改动。

- [ ] **Step 1: 运行 `tests/tdd/eval-convergence` 与已有 `tests/tdd/langfuse-v4-migration`。**
- [ ] **Step 2: 对三个 Deep Research Mission 运行 `--inspect`；不运行真实 Agent/Judge。**
- [ ] **Step 3: 运行相关 eval infrastructure incubating checks；只有定向失败无法界定时才扩大测试。**
- [ ] **Step 4: 运行 `git diff --check`、检查旧 Judge 正式引用和 `3.224.2` 当前态残留。**
- [ ] **Step 5: 只暂存并提交本任务文件，保留所有任务外工作区改动。**

