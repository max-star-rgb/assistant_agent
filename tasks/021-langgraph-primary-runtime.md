# Task 021 将 LangGraph 设为默认 Agent Runtime

## Goal

让 LangGraph 成为默认 Agent 编排入口，而不是旁路实现。

## Read first

- `docs/17-phase3-roadmap.md`
- `docs/18-langgraph-primary-runtime.md`
- `src/multimodal_agent/agent/workflow.py`
- `src/multimodal_agent/agent/graph.py`
- `src/multimodal_agent/agent/conditional_graph.py`
- `src/multimodal_agent/api/routes_agent.py`

## Scope

新增或调整 graph runtime，使：

```text
AgentWorkflow.run()
  ↓
AgentGraphRuntime.run()
  ↓
LangGraph compiled graph
```

## Requirements

- 保留 `AgentWorkflow.run()` 对外兼容。
- API 默认走 LangGraph runtime。
- 不删除旧 workflow 逻辑，先保留兼容。
- Runtime 依赖 ToolRegistry、MemoryStore、Config 应可注入。
- 默认仍使用 MockAdapter。
- 不调用真实外部服务。

## Suggested files

```text
src/multimodal_agent/agent/runtime.py
src/multimodal_agent/agent/workflow.py
src/multimodal_agent/api/routes_agent.py
tests/test_agent_runtime.py
tests/test_api_agent_graph_runtime.py
```

## Tests

至少覆盖：

- `AgentGraphRuntime.run()` 可返回 `AgentResponse`。
- `AgentWorkflow.run()` 仍返回兼容结果。
- API `/agent/run` 默认走 graph runtime。
- Search / Image / Render 基本路径可运行。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 022。
