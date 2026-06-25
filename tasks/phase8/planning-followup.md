# Phase 8B Task：Parallel Execution Strategies

## Goal

在 Assistant Loop MVP 稳定后，新增与 ReAct 平行的 `plan_and_solve` 显式执行策略。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/planning-and-reflection-roadmap.md
task/phase8/assistant-loop-mvp.md
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
src/multimodal_agent/agent/runtime.py
tests/
```

## Scope

新增或修改：

```text
execution_strategy request/state contract
plan_and_solve_graph
plan_and_solve_nodes
plan_validator
plan_status
current_step_id
plan_revision_count
tests / demo scenarios
strategy eval / review cases
```

## Requirements

- `plan_and_solve` 是与 ReAct 平行的执行策略，不是 ReAct 内部 action。
- 第一版只支持显式选择，默认 strategy 仍为 `react`，不做 `auto`。
- Planner 不能重新变成中心 router。
- 真实 LLM 路径不要复用旧 `RuleBasedTaskPlanner`。
- Planner / controller 由 LLM 输出结构化 JSON；代码只做 schema、工具白名单、预算、依赖、状态、trace 和调度。
- execute step 一次只能执行一个步骤，完成后必须回到 controller LLM。
- ReAct 和 Plan-and-Solve 必须共享 `ToolSpec` / `ActionValidator` / `ToolExecutor` / `ToolObservation` / trace / budget / memory 底座。
- 不删除旧 assistant loop。
- 不调用真实 Provider。
- 不写 API Key。
- 不做 reflection。
- 不做并发工具执行。
- 不修改 `tools/__init__.py`。
- 不本地 for-loop 自动执行完整计划。

## Tests

至少覆盖：

- 默认 strategy 是 `react`。
- 显式 `plan_and_solve` 才进入规划分支。
- planner 可以生成 plan。
- plan controller 可以驱动一个或多个 tool_call。
- 每次只执行一个步骤，工具 observation 返回 controller。
- plan 失败时可以安全停止或 ask_followup。
- 工具失败后可以 replan。
- 未知工具、循环依赖、步骤数量超限会被拒绝。
- ReAct 和 Plan-and-Solve 使用同一个 ToolExecutor。
- offline_eval 下不调用真实 Provider。
- `strategy` eval suite 覆盖默认 ReAct、显式 Plan-and-Solve、多步工具执行、计划拒绝和失败后重规划。
- strategy eval 必须使用 scripted chat adapter 模拟 LLM contract，不能复用旧规则 planner。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成 Phase 8B 后停止，不自动开始 Phase 8C。
