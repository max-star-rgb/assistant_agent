# Phase 8B Task：ReAct Plan Mode

## Goal

在 Assistant Loop MVP 稳定后，在同一个 ReAct assistant loop 中新增 plan mode。Plan mode 通过受控 `AssistantDecision` action 进入和退出，不新增与 ReAct 平行的 `plan_and_solve` 执行分支。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/planning-and-reflection-roadmap.md
tasks/phase8/assistant-loop-mvp.md
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
src/multimodal_agent/agent/runtime.py
tests/
```

## Scope

新增或修改：

```text
AssistantDecision plan-mode action contract
assistant_loop_nodes plan-mode state transitions
plan validator / plan state validator
plan_mode state
current_plan
plan_status
current_step_id
plan_revision_count
tests / demo scenarios
plan_mode eval / review cases
```

## Requirements

- Plan mode 是 ReAct 内部 action/state，不是独立 graph strategy。
- 不新增 `plan_and_solve_graph`、`plan_and_solve_nodes` 或新的 runtime graph selector。
- 不新增 `execution_strategy = "plan_and_solve"` 作为推荐路径；如仓库已有历史兼容字段，后续实现只能把它视为 legacy/compat 入口，内部仍回到 assistant loop。
- CLI/Web/API 可继续发送历史字段 `execution_strategy=plan_and_solve` 作为 plan-mode hint；它只提示同一个 ReAct assistant loop 优先考虑 `enter_plan_mode`。
- Planner 不能重新变成中心 router。
- 真实 LLM 路径不要复用旧 `RuleBasedTaskPlanner`。
- LLM 通过结构化 JSON 输出 `enter_plan_mode` / `exit_plan_mode` / `tool_call` / `ask_followup` / `final_answer`。
- `enter_plan_mode` 负责创建或修订当前计划，必须经过本地 schema、步骤数量、依赖、工具白名单和预算校验。
- `exit_plan_mode` 负责退出计划状态，并明确下一步是继续 ReAct、追问，还是交付最终回答。
- 工具执行一次只能执行一个 `tool_call`，完成后必须把 `ToolObservation` 返回同一个 `assistant_node`。
- 所有计划内工具执行必须共享 `ToolSpec` / `ActionValidator` / `ToolExecutor` / `ToolObservation` / trace / budget / memory 底座。
- 不删除旧 assistant loop。
- 不调用真实 Provider。
- 不写 API Key。
- 不做 reflection。
- 不做并发工具执行。
- 不修改 `tools/__init__.py`。
- 不本地 for-loop 自动执行完整计划。

## Tests

至少覆盖：

- 默认仍进入 assistant loop，不进入独立 planning graph。
- assistant 可以通过 `enter_plan_mode` 生成 plan。
- plan-mode 状态会记录 `current_plan` / `plan_status` / `current_step_id` / `plan_revision_count`。
- assistant 可以在 plan mode 中驱动一个或多个普通 `tool_call`。
- 每次只执行一个工具调用，工具 observation 返回同一个 assistant loop。
- plan 校验失败时可以安全停止或 ask_followup。
- 工具失败后可以修订当前计划。
- assistant 可以通过 `exit_plan_mode` 退出计划并生成最终回答或追问。
- 未知工具、循环依赖、步骤数量超限会被拒绝。
- plan mode 和普通 ReAct tool_call 使用同一个 ToolExecutor。
- offline_eval 下不调用真实 Provider。
- `plan_mode` eval suite 覆盖进入计划、多步工具执行、计划拒绝、失败后修订计划、退出计划。
- plan-mode eval 必须使用 scripted chat adapter 模拟 LLM contract，不能复用旧规则 planner。

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
