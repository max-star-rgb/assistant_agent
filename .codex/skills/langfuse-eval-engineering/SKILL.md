---
name: langfuse-eval-engineering
description: Use when designing, revising, running, or auditing assistant_agent pre-release Release Reviews, Langfuse Experiments, Score completeness, or evaluation infrastructure failures.
---

# Langfuse Release Review Engineering

先完整读取 `evals/README.md`。它与源码是事实权威；本 skill 只规定工作流。Release Review 是上线前
显式触发的短时验收，日常 trace 评分和线上长尾诊断属于 observability/runtime audit，不在这里实现。

## 1. 选择正确层

- 确定性代码契约：使用 pytest，并遵循 `$assistant-agent-development-testing`。
- 单个真实 Provider、Tool、Context、Memory 或本地模型连通性：使用 `evals/system/`。
- 待发布 Agent 的工具选择、漏用、参数、顺序、失败处理、grounding 和回答质量：使用
  `evals/release_review/`。

不要用多层重复验证同一事实，不要恢复已删除的 `evals/agent/tasks`、Python Environment、grader 或
calibration 路线。

## 2. 修改 Scenario

案例是 `evals/release_review/scenarios/*.yaml`，由 `contracts.py` 严格校验，Git 是唯一事实源。一个
scenario 只验证一个可命名风险，request 不暴露测试机关。

- **Decision**：使用完整生产工具目录；只用 YAML fixture 替换执行结果，不注册同名模拟 Tool，不改变
  catalog，也不做真实外部 Tool 调用。用 `required/allowed/forbidden`、arguments、sequence 和 state
  assertion 描述客观预期。critical Decision 必须运行两次。
- **Staging**：使用真实 Tool 实现及隔离资源；禁止 fixture。写操作必须 run-scoped、可验证、可清理；
  外部能力只开放批准的只读调用。

基础设施失败必须单独记录，不能转换成质量 Score 或 mock fallback。

## 3. Langfuse 原生运行

固定 Dataset 为 `assistant-agent-release-review`。一次 Release Review 创建一个原生 Dataset Run /
Experiment；Dataset item 由 Git scenario 和 repetitions 展开，未知或过期的 Git-owned item 不得静默
执行。

每个 item 必须有三个独立 BOOLEAN task-level Score：

- `assistant_agent.quality.task_conformance`：本地确定性断言；
- `assistant_agent.quality.grounding`：Langfuse Experiment Evaluator；
- `assistant_agent.quality.response_quality`：Langfuse Experiment Evaluator。

Staging observation 可额外产生 `assistant_agent.quality.tool_result_quality`。运行结束必须从 Langfuse
回查 observation 与三个 canonical Score；凭据、Dataset、Trace、Evaluator、资源、超时或 Score 缺失
均为 infrastructure failure。不要合成 reward 或让 UI 拥有发布权限。

## 4. 工作顺序

1. `python scripts/run_release_review.py --inspect`：离线验证 schema、案例数量和 repetitions。
2. 按 `tests/README.md` 运行 `tests/tdd/release-review-native-experiment/`。
3. `--sync`：在 operator 已配置的本机 Langfuse 同步固定 Dataset。
4. `--run`：仅在 real Provider、完整配置、`--allow-real-provider` 和
   `--allow-staging-side-effects` 都显式满足时运行；总预算 570 秒。
5. 审核 Langfuse Experiment、三个 Score、report 的 critical/high/flaky/infrastructure 分组和清理结果。
6. 用 `--record-decision` 记录人工 `approved`、`approved_with_risk` 或 `rejected`；程序不自动发布。

Langfuse UI 远程触发只能调用已签名的固定 Release Review webhook，再由服务端用固定 argv 启动同一个
CLI。UI 可选择 Dataset 和 Experiment Evaluator，但不能传环境变量、增加 scenario、扩大副作用权限或
绕过 readiness。精确 envelope、环境变量和产物位置只查 `evals/README.md`。

## 不变量

- pytest 始终 mock/local/offline；真实 Provider 不得静默回退 mock。
- API、CLI 和 webhook 复用同一 Release Review service 与 runtime，不复制 Agent loop。
- Decision fixture backend 只改变 Tool 执行结果，不改变注册表或 ToolSpec。
- Staging 资源必须 preflight、隔离、限并发并在失败路径清理。
- Git scenario hash、prompt version、catalog generation 和 evaluator version 不一致时，baseline 不可比较。
- 不提交凭据、真实用户数据、Provider 原始响应、生产 Trace 或 `.data/evals/` 产物。
