# Task 002：AgentState

## Goal

实现 AgentState 和状态更新辅助方法，确保一次用户请求的完整流程可以被结构化追踪。

## Read first

- `docs/03-agent-state.md`
- `docs/08-testing.md`

## Scope

新增/修改：

```text
src/multimodal_agent/agent/state.py
tests/unit/test_agent_state.py
```

## Steps

1. 实现 `AgentState`。
2. 实现创建 run_id/session_id 的工具函数。
3. 实现 `add_tool_call`、`complete_tool_call`、`fail_tool_call`。
4. 实现 `set_intent`、`set_plan`、`set_response`。
5. 测试状态从 created → running → completed/failed 的转换。

## Acceptance

```bash
pytest tests/unit/test_agent_state.py
```

必须通过。

## Out of scope

- 不实现具体工具。
- 不实现复杂工作流。
