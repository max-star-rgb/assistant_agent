# Task 014 LangGraph 最小接入

## Goal

把当前自研同步 workflow 最小化接入 LangGraph，让项目中真正出现可执行的 StateGraph。

## Read first

- `docs/11-langgraph-integration.md`
- `src/multimodal_agent/agent/workflow.py`
- 现有 `AgentState`、intent、tool registry 相关文件

## Scope

实现最小 LangGraph：

```text
START
  ↓
detect_intent
  ↓
route_tools
  ↓
execute_tools
  ↓
compose_response
  ↓
END
```

## Requirements

- 新增 `build_agent_graph()`。
- 使用 `StateGraph`、`START`、`END`、`.compile()`。
- 复用现有 workflow 逻辑，不重复实现业务规则。
- 保留 `AgentWorkflow.run()` 兼容入口。
- 如果 LangGraph 未安装，不要联网安装；先停止并提示依赖缺失。

## Suggested files

优先：

```text
src/multimodal_agent/agent/workflow.py
```

如代码较长，可以新增：

```text
src/multimodal_agent/agent/graph.py
src/multimodal_agent/agent/graph_nodes.py
```

## Tests

新增或更新：

```text
tests/test_langgraph_workflow.py
```

至少测试：

- graph 可以 compile。
- graph 可以处理简单 query。
- `AgentWorkflow.run()` 仍可用。
- 现有 mock tools 不调用外部服务。

## Acceptance

运行：

```bash
python -m pytest
```

并检查代码中存在：

```text
StateGraph
START
END
compile
```

## Stop condition

完成后停止，不要继续 Task 015。
