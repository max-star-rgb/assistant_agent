# Phase 8A.2：ReAct Final Answer Handoff Fix

## Goal

修复真实 ReAct assistant loop 中 `final_answer` 被本地 composer 覆盖的问题。

## Read first

- `AGENTS.md`
- `docs/phase8/phase8A_2_react_final_answer_handoff.md`
- `src/multimodal_agent/agent/assistant_loop_nodes.py`
- `src/multimodal_agent/agent/assistant_loop_graph.py`
- `src/multimodal_agent/agent/response_composer.py`
- `scripts/demo_assistant_loop.py`

## Scope

- 只修改 assistant loop 的 final answer handoff。
- 可补充 demo 输出字段，帮助观察真实 ReAct 流程。
- 可新增回归测试。

## Requirements

- 真实 chat provider 或 scripted non-mock adapter 在 observation 后返回 `final_answer` 时，必须设置 `AgentState.response`。
- mock/offline rule plan 的 synthetic `final_answer` 仍交给 composer。
- response data 必须保留工具调用上下文和 contracts。
- 不调用真实 Provider。
- 不写 API Key。
- 不修改 `tools/__init__.py`。

## Acceptance

- 新增测试覆盖 non-mock adapter 的 `tool_call -> observation -> final_answer`。
- 新增测试覆盖 mock/offline composer 路径不被破坏。
- `python -m pytest` 通过。
- `scripts/run_evals.py` 通过。
- `scripts/run_demo_flows.py` 通过。

## Stop condition

测试通过后停止，汇报修改摘要、测试结果和剩余风险。
