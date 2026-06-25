# Phase 8C Task：Reflection Follow-up

## Goal

在 Assistant Loop 和 Planning 稳定后，增加 reflection 节点，用于工具失败、低置信度和循环风险处理。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/planning-and-reflection-roadmap.md
tasks/phase8/assistant-loop-mvp.md
tasks/phase8/planning-followup.md
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
tests/
```

## Scope

新增或修改：

```text
reflection_node
ReflectionResult schema
reflection trigger policy
reflection tests / demo scenarios
```

## Requirements

- Reflection 不直接执行工具。
- Reflection 只能 revise decision / ask_followup / final_answer with caveat / stop。
- 不调用真实 Provider。
- 不写 API Key。
- 不部署到公网。
- 不修改 `tools/__init__.py`。

## Tests

至少覆盖：

- tool failure 触发 reflection。
- unknown tool 触发 reflection 或安全停止。
- low confidence 触发 ask_followup。
- loop limit approaching 时可以停止。
- reflection 不直接执行工具。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成 Phase 8C 后停止，等待用户决定后续阶段。
