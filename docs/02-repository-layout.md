# 02 工程目录结构

## 1. 推荐目录

```text
src/multimodal_agent/
├── __init__.py
├── api/
│   ├── app.py
│   ├── routes_agent.py
│   ├── routes_tools.py
│   └── websocket.py
├── agent/
│   ├── state.py
│   ├── planner.py
│   ├── intent.py
│   ├── router.py
│   ├── executor.py
│   └── workflow.py
├── schemas/
│   ├── base.py
│   ├── requests.py
│   ├── perception.py
│   ├── tools.py
│   ├── products.py
│   ├── generation.py
│   ├── rendering.py
│   └── memory.py
├── tools/
│   ├── base.py
│   ├── registry.py
│   ├── vision_tool.py
│   ├── product_search_tool.py
│   ├── price_compare_tool.py
│   ├── image_generation_tool.py
│   ├── render_tool.py
│   └── memory_tool.py
├── memory/
│   ├── store.py
│   ├── retriever.py
│   └── schemas.py
├── services/
│   ├── vision_adapter.py
│   ├── product_adapter.py
│   ├── image_generation_adapter.py
│   └── render_adapter.py
└── utils/
    ├── ids.py
    └── time.py
```

## 2. 模块职责

- `schemas/`：所有跨模块数据结构。
- `agent/`：Agent 自主决策、任务规划、工具路由。
- `tools/`：统一工具接口及工具实现。
- `services/`：对真实外部服务或 mock 服务的适配。
- `memory/`：记忆写入、检索、排序。
- `api/`：HTTP/WebSocket 对外入口。
- `tests/`：单元测试、集成测试、端到端测试。

## 3. 文件边界

- 不要让 `api/` 直接调用外部模型，必须经过 `agent/` 或 `tools/`。
- 不要让 `agent/` 直接写数据库，必须经过 `memory/` 或工具。
- 不要让具体供应商 SDK 泄漏到 schema 和 AgentState 中。
- mock adapter 与真实 adapter 使用同一接口。

## 4. 测试目录

```text
tests/
├── unit/
│   ├── test_schemas.py
│   ├── test_intent.py
│   ├── test_router.py
│   ├── test_tool_registry.py
│   └── test_memory.py
├── integration/
│   ├── test_agent_workflow.py
│   └── test_api_agent.py
└── e2e/
    └── test_demo_flow.py
```
