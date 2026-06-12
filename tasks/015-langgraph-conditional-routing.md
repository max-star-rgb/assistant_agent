# Task 015 LangGraph 条件路由

## Goal

在 Task 014 的基础上，把 Tool Router 变成 LangGraph 条件边，而不是单纯线性执行。

## Read first

- `docs/11-langgraph-integration.md`
- `docs/04-intent-and-routing.md`
- 当前 LangGraph 文件
- Tool registry 和 intent router 实现

## Scope

实现条件路由：

```text
detect_intent
  ↓
route_by_intent
  ├─ vision_node
  ├─ search_node
  ├─ compare_node
  ├─ image_generation_node
  ├─ render_node
  ├─ memory_node
  └─ chat_node
```

## Requirements

- 使用 `add_conditional_edges`。
- 路由函数只返回节点名，不直接执行业务。
- 每个节点内部调用现有 Tool/Adapter。
- 未识别 intent 进入 chat 或 compose_response。
- 不新增真实 Provider。

## Tests

新增或更新：

```text
tests/test_langgraph_routing.py
```

至少覆盖：

- search intent 路由到 search node。
- image generation intent 路由到 image node。
- render intent 路由到 render node。
- unknown intent 不崩溃。

## Acceptance

```bash
python -m pytest
```

并确认出现：

```text
add_conditional_edges
```

## Stop condition

完成后停止，不要继续 Task 016。
