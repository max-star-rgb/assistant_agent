# Phase 8B Task：Planning Follow-up

## Goal

在 Assistant Loop MVP 稳定后，为 assistant loop 增加 planning action。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/planning-and-reflection-roadmap.md
task/phase8/assistant-loop-mvp.md
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
tests/
```

## Scope

新增或修改：

```text
plan_node
PlanStep / AssistantPlan schema
current_plan
current_step_index
plan_status
assistant decision type: plan
tests / demo scenarios
```

## Requirements

- Planner 不能重新变成中心 router。
- Planning 只是 assistant 可选择的 action。
- 不删除旧 assistant loop。
- 不调用真实 Provider。
- 不写 API Key。
- 不做 reflection。
- 不做并发工具执行。
- 不修改 `tools/__init__.py`。

## Tests

至少覆盖：

- assistant 可以生成 plan。
- plan 可以驱动一个或多个 tool_call。
- plan 不绕过 assistant_node。
- plan 失败时可以安全停止或 ask_followup。
- offline_eval 下不调用真实 Provider。

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
