# Task 001：领域 Schema

## Goal

定义项目核心 Pydantic 数据结构，为 AgentState、工具调用、感知结果、商品、图片生成、渲染、记忆提供统一类型。

## Read first

- `docs/01-architecture.md`
- `docs/03-agent-state.md`
- `docs/05-tool-contracts.md`

## Scope

新增/修改：

```text
src/multimodal_agent/schemas/
tests/unit/test_schemas.py
```

## Steps

1. 定义 `UserRequest`、`AgentResponse`。
2. 定义 `VisualUnderstandingResult`、`PerceptionBundle`。
3. 定义 `IntentResult`、`TaskPlan`、`TaskStep`。
4. 定义 `ToolSelection`、`ToolResult`、`ToolCallRecord`。
5. 定义 `ProductResult`、`PriceCompareResult`。
6. 定义 `ImageGenerationResult`、`RenderResult`。
7. 定义 `MemoryItem`。
8. 为关键字段写 Pydantic 校验测试。

## Acceptance

```bash
pytest tests/unit/test_schemas.py
```

必须通过，并能序列化/反序列化核心 schema。

## Out of scope

- 不实现工具逻辑。
- 不实现 API。
