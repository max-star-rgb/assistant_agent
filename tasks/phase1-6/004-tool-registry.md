# Task 004：Tool Registry 与 Mock Tools

## Goal

建立统一工具注册与执行机制，并实现稳定的 mock tools。

## Read first

- `docs/05-tool-contracts.md`
- `docs/04-intent-and-routing.md`

## Scope

新增/修改：

```text
src/multimodal_agent/tools/base.py
src/multimodal_agent/tools/registry.py
src/multimodal_agent/tools/*_tool.py
tests/unit/test_tool_registry.py
tests/unit/test_mock_tools.py
```

## Steps

1. 定义 `BaseTool`。
2. 实现 `ToolRegistry.register/get/list`。
3. 实现 mock 工具：vision、product_search、price_compare、image_generation、render_3d、memory。
4. 工具返回统一 `ToolResult`。
5. 工具失败时返回结构化 error。

## Acceptance

```bash
pytest tests/unit/test_tool_registry.py tests/unit/test_mock_tools.py
```

必须通过。

## Out of scope

- 不接入真实外部 API。
- 不实现异步任务队列。
