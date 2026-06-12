# Task 016 多步任务规划

## Goal

支持一个用户请求触发多个工具步骤，例如“识别视频商品 → 搜索 → 比价 → 生成图片”。

## Read first

- `docs/12-multistep-planning.md`
- 当前 AgentState schema
- 当前 LangGraph 实现
- ToolResult / ToolCall schema

## Scope

新增最小 TaskPlan 能力。

推荐新增：

```text
src/multimodal_agent/schemas/planning.py
```

包含：

```text
PlanStep
TaskPlan
```

## Requirements

- 先实现规则规划，不调用 LLM 规划。
- 如果有图片/视频输入，优先加入 vision step。
- 根据 query 关键词加入 search、compare、image_generation、render。
- 每一步结果写入 AgentState 或 tool_results。
- 支持至少 3 步连续执行。

## Tests

新增：

```text
tests/test_multistep_planning.py
```

覆盖：

```text
"找视频里的鞋子，比较价格，再生成海报"
```

期望工具顺序包含：

```text
vision_understanding
product_search
price_compare
image_generation
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 017。
