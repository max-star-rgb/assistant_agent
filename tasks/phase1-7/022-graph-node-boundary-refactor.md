# Task 022 拆分 Graph Node 与 Workflow 私有方法边界

## Goal

让 LangGraph 节点不再依赖 `AgentWorkflow` 的私有方法，降低 graph 与 workflow 耦合。

## Read first

- `docs/19-graph-node-boundaries.md`
- 当前 `workflow.py`
- 当前 `graph.py`
- 当前 `conditional_graph.py`

## Scope

抽出公共组件：

```text
tool_input_builder.py
tool_executor.py
response_composer.py
graph_nodes.py
```

实际文件名可根据当前项目风格调整。

## Requirements

- graph nodes 不调用 `_build_tool_input()`、`_run_tool()`、`_compose_response()`。
- 私有方法逻辑迁移为公共函数或服务类。
- workflow.py 只保留兼容包装或轻量 orchestration。
- 不改变外部 API 行为。
- 不新增真实 Provider。

## Tests

新增或更新：

```text
tests/test_tool_input_builder.py
tests/test_tool_executor.py
tests/test_response_composer.py
tests/test_graph_node_boundaries.py
```

## Acceptance

```bash
python -m pytest
```

并确认 graph node 不再引用 workflow 私有方法。

## Stop condition

完成后停止，不要继续 Task 023。
