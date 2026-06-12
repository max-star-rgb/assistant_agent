# 11 LangGraph 集成设计

## 目标

当前项目 000-013 已完成，但 Agent 编排仍是自研同步 workflow，主要位于：

```text
src/multimodal_agent/agent/workflow.py
```

下一阶段目标是把 Agent 编排层迁移到 LangGraph，使系统真正具备显式状态图、节点化任务流、条件路由、可测试执行路径，并为后续多步规划、循环、人工确认、异步任务打基础。

## 当前状态

Codex 已检查以下关键字：

```text
StateGraph
START
END
add_node
add_edge
add_conditional_edges
.compile(
```

结果：无匹配项。

所以当前代码尚未真正接入 LangGraph。

## 最小接入原则

不要大改现有 AgentWorkflow。

推荐做法：

1. 保留现有 `AgentWorkflow.run()` 对外兼容。
2. 新增 `build_agent_graph()`。
3. 使用现有 `AgentState` 作为状态载体。
4. 把现有同步流程拆成 LangGraph 节点。
5. 先实现线性图，再实现条件路由。
6. 所有节点优先复用现有 service、router、registry、tool。

## 推荐节点

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

## 推荐文件

优先：

```text
src/multimodal_agent/agent/workflow.py
```

如果代码变长，可以拆成：

```text
src/multimodal_agent/agent/graph.py
src/multimodal_agent/agent/graph_nodes.py
```

并让 `workflow.py` 保留兼容包装。

## 依赖处理

如果项目尚未安装 LangGraph，不要自动联网安装。

先检查：

```bash
python -c "import langgraph; print(langgraph.__version__)"
```

如果失败，停止并提示用户安装：

```bash
pip install langgraph
```

## 验收标准

- 代码中出现 `StateGraph`、`START`、`END`、`.compile()`。
- 至少有一个测试证明 graph 可以执行。
- 现有 `AgentWorkflow.run()` 仍可工作。
- 现有 000-013 的测试不应破坏。
- 不引入真实外部模型调用。
