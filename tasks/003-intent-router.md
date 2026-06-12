# Task 003：意图识别与路由

## Goal

实现初版意图识别器和 Tool Router，使 Agent 能根据用户指令选择下一步工具或追问。

## Read first

- `docs/04-intent-and-routing.md`
- `docs/03-agent-state.md`
- `docs/05-tool-contracts.md`

## Scope

新增/修改：

```text
src/multimodal_agent/agent/intent.py
src/multimodal_agent/agent/router.py
tests/unit/test_intent.py
tests/unit/test_router.py
```

## Steps

1. 实现规则版 `IntentDetector`。
2. 根据文本关键词和媒体输入判断 intent。
3. 实现 `ToolRouter`，把 intent 映射到工具名。
4. 支持多工具任务：视频理解 → 搜索 → 比价 → 图片生成。
5. 信息不足时返回 `ask_followup`。

## Acceptance

```bash
pytest tests/unit/test_intent.py tests/unit/test_router.py
```

必须覆盖以下用例：

- “图里是什么” → understand_image
- “视频里发生了什么” → understand_video
- “找相似款” → search_product
- “哪个便宜” → compare_price
- “生成海报” → generate_image
- “放到客厅看看” → render_3d
- “上次那个黑色包” → retrieve_memory
- “找视频里的鞋子，比价，再生成海报” → multi_tool_task

## Out of scope

- 不调用真实 LLM。
- 不执行工具。
