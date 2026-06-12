# Task 013：端到端 Demo 流程

## Goal

完成一个从用户请求到多工具执行再到结果返回和记忆保存的完整 Demo。

## Read first

- `docs/01-architecture.md`
- `docs/04-intent-and-routing.md`
- `docs/05-tool-contracts.md`
- `docs/06-memory-design.md`
- `docs/08-testing.md`

## Scope

新增/修改：

```text
tests/e2e/test_demo_flow.py
README.md
```

必要时小幅修复已有模块，但不要新增未规划的大功能。

## Demo 场景

用户：

```text
帮我找视频里的鞋子，比较价格，然后生成一张日系海报。
```

输入：

```json
{
  "user_id": "u1",
  "session_id": "s1",
  "text": "帮我找视频里的鞋子，比较价格，然后生成一张日系海报。",
  "video_ids": ["video_demo_1"]
}
```

预期执行：

1. VisionUnderstandingTool
2. ProductSearchTool
3. PriceCompareTool
4. ImageGenerationTool
5. MemoryTool.save
6. ResponseComposer

## Acceptance

```bash
pytest tests/e2e/test_demo_flow.py
```

必须验证：

- 返回 intent = multi_tool_task。
- tool_calls 至少包含 vision、product_search、price_compare、image_generation。
- final response 中包含商品、价格、图片生成结果。
- 记忆中保存本次任务摘要。

## Out of scope

- 不真实生成图片。
- 不真实访问电商平台。
- 不真实渲染 3D。
