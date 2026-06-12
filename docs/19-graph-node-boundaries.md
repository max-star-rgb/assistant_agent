# 19 Graph Node 边界设计

## 背景

Phase 2 审计指出：LangGraph 节点可能仍调用 `_build_tool_input()`、`_run_tool()`、`_compose_response()` 等 workflow 私有方法。

这会导致 graph 节点难以单测、workflow 和 graph 互相耦合、后续替换 runtime 困难、节点复用性差。

## 目标

把 graph 节点依赖的能力抽成公共服务函数或类，而不是依赖 workflow 私有方法。

## 推荐拆分

```text
src/multimodal_agent/agent/
├── runtime.py
├── graph.py
├── graph_nodes.py
├── tool_input_builder.py
├── tool_executor.py
├── response_composer.py
└── workflow.py
```

## 职责划分

### graph.py

只负责构建 StateGraph：add_node、add_edge、add_conditional_edges、compile。

### graph_nodes.py

只负责 LangGraph node 函数：detect_intent_node、route_tools_node、execute_tool_node、compose_response_node。

### tool_input_builder.py

负责将 AgentState 转为 Tool 输入。

### tool_executor.py

负责调用 ToolRegistry，并记录 ToolCall / ToolResult。

### response_composer.py

负责把 AgentState 合成为 AgentResponse。

### workflow.py

只保留兼容包装，不再承载核心业务逻辑。

## 验收标准

- graph node 不调用 workflow 私有方法。
- workflow.py 行数和职责明显收敛。
- tool input、tool execution、response compose 都有独立测试。
