# 18 LangGraph 默认运行时设计

## 目标

当前 LangGraph 已存在，但还是旁路实现。Phase 3 要让 LangGraph 成为默认 Agent Runtime。

## 当前问题

当前结构大致为：

```text
API / tests
  ↓
AgentWorkflow.run()
  ↓
自研同步 workflow
```

LangGraph 位于：

```text
src/multimodal_agent/agent/graph.py
src/multimodal_agent/agent/conditional_graph.py
```

但默认路径没有统一走 graph。

## 目标结构

```text
API / CLI / tests
  ↓
AgentWorkflow.run()
  ↓
AgentGraphRuntime.invoke()
  ↓
LangGraph compiled graph
  ↓
Tool Registry / Adapter / Memory / Response
```

## 兼容策略

保留 AgentWorkflow.run()，但内部改为调用 graph runtime。

不推荐直接删除旧 workflow 私有方法。先迁移，再清理。

## 推荐新增对象

```python
class AgentGraphRuntime:
    def __init__(self, registry, memory_store=None, config=None):
        ...

    def run(self, request: UserRequest) -> AgentResponse:
        ...
```

推荐位置：

```text
src/multimodal_agent/agent/runtime.py
```

## API 层要求

FastAPI route 不直接调用 graph.py。

推荐：

```text
routes_agent.py
  ↓
get_agent_runtime()
  ↓
AgentGraphRuntime.run()
```

## 测试要求

必须覆盖：

- API 默认走 graph runtime。
- AgentWorkflow.run() 仍兼容。
- graph runtime 可以处理 search/image/render/memory 基本路径。
- 旧同步路径不再是默认路径。

## 禁止事项

- 不要在 API route 里直接写 graph 节点逻辑。
- 不要让 Tool 直接调用 Provider。
- 不要为了接入 graph 删除已有测试。
